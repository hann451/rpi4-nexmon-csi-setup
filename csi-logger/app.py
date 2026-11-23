#!/usr/bin/env python3
"""
Nexmon CSI continuous capture, Google Drive (rclone) auto-sync,
and Tailscale-accessible smartphone ground-truth labeling Web UI.
Target runtime: Python 3.9+ on Raspberry Pi OS Bullseye (standard library only).
"""

import csv
import datetime
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DATA_DIR = Path(os.environ.get("CSI_DATA_DIR", "/home/pi/csi_data"))
STATE_FILE = DATA_DIR / "state.json"
LABELS_CSV = DATA_DIR / "labels.csv"
LABELS_JSONL = DATA_DIR / "labels.jsonl"
SEGMENTS_CSV = DATA_DIR / "segments.csv"

CSI_CHANSPEC = os.environ.get("CSI_CHANSPEC", "36/80")
CSI_CORE = os.environ.get("CSI_CORE", "1")
CSI_NSS = os.environ.get("CSI_NSS", "1")
CSI_ROTATE_SEC = int(os.environ.get("CSI_ROTATE_SEC", "300"))
CSI_PORT = int(os.environ.get("CSI_PORT", "8080"))
MIN_FREE_BYTES = int(float(os.environ.get("CSI_MIN_FREE_GB", "2.0")) * (1024 ** 3))

RCLONE_REMOTE = os.environ.get("RCLONE_REMOTE", "gdrive:rpi4_nexmon_csi")
RCLONE_CONFIG = os.environ.get("RCLONE_CONFIG", "/home/pi/.config/rclone/rclone.conf")
RCLONE_SYNC_INTERVAL_SEC = int(os.environ.get("RCLONE_SYNC_INTERVAL_SEC", "60"))

PRESENCE_VALUES = {"in_room", "away"}
WINDOW_VALUES = {"open", "closed"}
DATE_RE = re.compile(r"csi_(\d{4})(\d{2})(\d{2})_")

LABEL_FIELDS = [
    "timestamp_iso",
    "epoch_ms",
    "date",
    "presence",
    "window",
    "label_class",
    "memo",
    "event_type",
    "segment_id",
    "relative_path",
]

SEGMENT_FIELDS = [
    "segment_id",
    "date",
    "relative_path",
    "filename",
    "start_iso",
    "end_iso",
    "start_epoch_ms",
    "end_epoch_ms",
    "duration_sec",
    "bytes",
    "presence",
    "window",
    "label_class",
    "memos",
    "chanspec",
]


def now_local() -> datetime.datetime:
    return datetime.datetime.now().astimezone()


def iso_now() -> str:
    return now_local().isoformat(timespec="seconds")


def make_label_class(presence: str, window: str) -> str:
    return f"{presence}__{window}"


def infer_date_from_path(path: Path) -> str:
    m = DATE_RE.search(path.name)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    mtime_dt = datetime.datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    return mtime_dt.strftime("%Y-%m-%d")


def run_cmd(cmd: List[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )


class CSILoggerService:
    def __init__(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.rotate_event = threading.Event()
        self.sync_event = threading.Event()

        self.state: Dict[str, Any] = self._load_state()
        self.active_pcap_path: Optional[Path] = None
        self.active_pcap_start: Optional[datetime.datetime] = None
        self.active_segment_id: str = ""
        self.active_segment_memos: List[str] = []
        self.capture_proc: Optional[subprocess.Popen] = None
        self.last_chanspec_output: str = ""
        self.capture_status: str = "initializing"
        self.capture_error: str = ""

        self.sync_status: Dict[str, Any] = {
            "rclone_configured": False,
            "remote": RCLONE_REMOTE,
            "syncing": False,
            "last_sync_at": None,
            "last_sync_ok": None,
            "last_sync_msg": "未同期",
            "uploaded_count": int(self.state.get("uploaded_count", 0)),
        }

        self._recover_orphan_active_pcaps()

    def _load_state(self) -> Dict[str, Any]:
        default_state: Dict[str, Any] = {
            "presence": "in_room",
            "window": "closed",
            "memo": "",
            "capture_enabled": True,
            "updated_at": iso_now(),
            "epoch_ms": int(time.time() * 1000),
            "uploaded_count": 0,
        }
        if STATE_FILE.exists():
            try:
                loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                for k, v in default_state.items():
                    loaded.setdefault(k, v)
                if loaded.get("presence") not in PRESENCE_VALUES:
                    loaded["presence"] = "in_room"
                if loaded.get("window") not in WINDOW_VALUES:
                    loaded["window"] = "closed"
                return loaded
            except Exception:
                pass
        return default_state

    def _save_state_locked(self) -> None:
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)

    def _recover_orphan_active_pcaps(self) -> None:
        """Finalize leftover .pcap.active files and migrate any legacy DATA_DIR/pcap files into date folders."""
        candidates: List[Path] = list(DATA_DIR.glob("*/pcap/*.pcap.active"))
        legacy_dir = DATA_DIR / "pcap"
        if legacy_dir.exists():
            candidates.extend(legacy_dir.glob("*.pcap*"))

        for p in sorted(set(candidates)):
            try:
                if not p.exists() or not p.is_file():
                    continue
                date_str = infer_date_from_path(p)
                target_pcap_dir = DATA_DIR / date_str / "pcap"
                target_pcap_dir.mkdir(parents=True, exist_ok=True)

                if p.name.endswith(".pcap.active"):
                    if p.stat().st_size <= 24:
                        p.unlink(missing_ok=True)
                        continue
                    final_name = p.name[: -len(".active")]
                    dest = target_pcap_dir / final_name
                    if dest.exists():
                        dest = target_pcap_dir / f"{dest.stem}_recovered.pcap"
                    p.rename(dest)
                elif p.name.endswith(".pcap") and p.parent == legacy_dir:
                    dest = target_pcap_dir / p.name
                    if dest.exists():
                        dest = target_pcap_dir / f"{dest.stem}_migrated.pcap"
                    p.rename(dest)
            except Exception:
                pass

    def _append_label_records_locked(self, event_type: str, memo_text: str) -> None:
        now_dt = now_local()
        ts_iso = now_dt.isoformat(timespec="seconds")
        epoch_ms = int(now_dt.timestamp() * 1000)
        date_str = now_dt.strftime("%Y-%m-%d")
        presence = self.state["presence"]
        window = self.state["window"]
        label_class = make_label_class(presence, window)

        rel_path = ""
        if self.active_pcap_path is not None:
            try:
                rel_path = self.active_pcap_path.relative_to(DATA_DIR).as_posix()
            except ValueError:
                rel_path = self.active_pcap_path.name

        row = {
            "timestamp_iso": ts_iso,
            "epoch_ms": epoch_ms,
            "date": date_str,
            "presence": presence,
            "window": window,
            "label_class": label_class,
            "memo": memo_text,
            "event_type": event_type,
            "segment_id": self.active_segment_id,
            "relative_path": rel_path,
        }

        day_dir = DATA_DIR / date_str
        day_dir.mkdir(parents=True, exist_ok=True)
        daily_csv = day_dir / f"labels_{date_str}.csv"

        for csv_path in (LABELS_CSV, daily_csv):
            need_header = not csv_path.exists() or csv_path.stat().st_size == 0
            with csv_path.open("a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=LABEL_FIELDS)
                if need_header:
                    writer.writeheader()
                writer.writerow(row)

        with LABELS_JSONL.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _append_segment_record(
        self,
        segment_id: str,
        date_str: str,
        relative_path: str,
        filename: str,
        start_dt: datetime.datetime,
        end_dt: datetime.datetime,
        size_bytes: int,
        presence: str,
        window: str,
        memos: List[str],
    ) -> None:
        day_dir = DATA_DIR / date_str
        day_dir.mkdir(parents=True, exist_ok=True)
        daily_seg_csv = day_dir / f"segments_{date_str}.csv"

        row = {
            "segment_id": segment_id,
            "date": date_str,
            "relative_path": relative_path,
            "filename": filename,
            "start_iso": start_dt.isoformat(timespec="seconds"),
            "end_iso": end_dt.isoformat(timespec="seconds"),
            "start_epoch_ms": int(start_dt.timestamp() * 1000),
            "end_epoch_ms": int(end_dt.timestamp() * 1000),
            "duration_sec": round((end_dt - start_dt).total_seconds(), 2),
            "bytes": size_bytes,
            "presence": presence,
            "window": window,
            "label_class": make_label_class(presence, window),
            "memos": " | ".join(memos),
            "chanspec": CSI_CHANSPEC,
        }

        for csv_path in (SEGMENTS_CSV, daily_seg_csv):
            need_header = not csv_path.exists() or csv_path.stat().st_size == 0
            with csv_path.open("a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=SEGMENT_FIELDS)
                if need_header:
                    writer.writeheader()
                writer.writerow(row)

    def ensure_csi_interface(self) -> bool:
        """Ensure wlan0 and mon0 are up and Nexmon CSI extractor is configured."""
        try:
            run_cmd(["systemctl", "stop", "wpa_supplicant"], timeout=10)
            run_cmd(["pkill", "wpa_supplicant"], timeout=5)
            time.sleep(0.5)

            res_params = run_cmd(
                ["makecsiparams", "-c", CSI_CHANSPEC, "-C", CSI_CORE, "-N", CSI_NSS],
                timeout=10,
            )
            if res_params.returncode != 0 or not res_params.stdout.strip():
                with self.lock:
                    self.capture_error = f"makecsiparams failed: {res_params.stderr.strip()}"
                return False
            csiparams = res_params.stdout.strip()

            run_cmd(["ip", "link", "set", "wlan0", "up"], timeout=10)
            res_nex = run_cmd(
                ["nexutil", "-Iwlan0", "-s500", "-b", "-l34", f"-v{csiparams}"],
                timeout=10,
            )
            if res_nex.returncode != 0:
                with self.lock:
                    self.capture_error = f"nexutil config failed: {res_nex.stderr.strip()}"
                return False

            res_mon_check = run_cmd(["ip", "link", "show", "mon0"], timeout=5)
            if res_mon_check.returncode != 0:
                res_iw = run_cmd(["iw", "dev", "wlan0", "info"], timeout=5)
                wiphy = "phy0"
                for line in res_iw.stdout.splitlines():
                    parts = line.strip().split()
                    if len(parts) >= 2 and parts[0] == "wiphy":
                        wiphy = f"phy{parts[1]}"
                        break
                run_cmd(["iw", "phy", wiphy, "interface", "add", "mon0", "type", "monitor"], timeout=10)

            run_cmd(["ip", "link", "set", "mon0", "up"], timeout=10)
            res_k = run_cmd(["nexutil", "-Iwlan0", "-k"], timeout=10)
            with self.lock:
                self.last_chanspec_output = res_k.stdout.strip()
                self.capture_error = ""
            return True
        except Exception as exc:
            with self.lock:
                self.capture_error = f"interface init error: {exc}"
            return False

    def _start_tcpdump(self) -> Optional[subprocess.Popen]:
        with self.lock:
            presence = self.state["presence"]
            window = self.state["window"]
            start_dt = now_local()
            date_str = start_dt.strftime("%Y-%m-%d")
            segment_id = start_dt.strftime("%Y%m%d_%H%M%S")
            pcap_dir = DATA_DIR / date_str / "pcap"
            pcap_dir.mkdir(parents=True, exist_ok=True)
            fname = f"csi_{segment_id}_{presence}_{window}.pcap.active"
            path = pcap_dir / fname

            self.active_pcap_path = path
            self.active_pcap_start = start_dt
            self.active_segment_id = segment_id
            self.active_segment_memos = []
            self.capture_status = "recording"

        proc = subprocess.Popen(
            ["tcpdump", "-i", "wlan0", "-n", "dst", "port", "5500", "-U", "-w", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        with self.lock:
            self.capture_proc = proc
        return proc

    def _stop_and_finalize_tcpdump(self, seg_presence: str, seg_window: str) -> None:
        with self.lock:
            proc = self.capture_proc
            path = self.active_pcap_path
            start_dt = self.active_pcap_start
            segment_id = self.active_segment_id
            memos = list(self.active_segment_memos)
            self.capture_proc = None

        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)

        end_dt = now_local()
        if path is not None and path.exists():
            size_bytes = path.stat().st_size
            if size_bytes > 24:
                final_path = Path(str(path)[: -len(".active")])
                if final_path.exists():
                    final_path = path.parent / f"{final_path.stem}_{int(time.time())}.pcap"
                path.rename(final_path)
                if start_dt is not None:
                    date_str = start_dt.strftime("%Y-%m-%d")
                    rel_path = final_path.relative_to(DATA_DIR).as_posix()
                    self._append_segment_record(
                        segment_id=segment_id or start_dt.strftime("%Y%m%d_%H%M%S"),
                        date_str=date_str,
                        relative_path=rel_path,
                        filename=final_path.name,
                        start_dt=start_dt,
                        end_dt=end_dt,
                        size_bytes=size_bytes,
                        presence=seg_presence,
                        window=seg_window,
                        memos=memos,
                    )
            else:
                path.unlink(missing_ok=True)

        with self.lock:
            self.active_pcap_path = None
            self.active_pcap_start = None
            self.active_segment_id = ""
            self.active_segment_memos = []

    def capture_loop(self) -> None:
        iface_ready = False
        while not self.stop_event.is_set():
            with self.lock:
                enabled = bool(self.state.get("capture_enabled", True))
                seg_presence = self.state["presence"]
                seg_window = self.state["window"]

            if not enabled:
                with self.lock:
                    self.capture_status = "paused"
                iface_ready = False
                self.rotate_event.wait(timeout=1.0)
                self.rotate_event.clear()
                continue

            free_bytes = shutil.disk_usage(DATA_DIR).free
            if free_bytes < MIN_FREE_BYTES:
                with self.lock:
                    self.capture_status = "disk_low_paused"
                    self.capture_error = f"空き容量不足 ({free_bytes / (1024**3):.2f} GB)"
                self.stop_event.wait(timeout=15.0)
                continue

            if not iface_ready or run_cmd(["ip", "link", "show", "mon0"], timeout=5).returncode != 0:
                iface_ready = self.ensure_csi_interface()
                if not iface_ready:
                    with self.lock:
                        self.capture_status = "interface_error"
                    self.stop_event.wait(timeout=5.0)
                    continue

            proc = self._start_tcpdump()
            if proc is None:
                self.stop_event.wait(timeout=3.0)
                continue

            started_mono = time.monotonic()
            while not self.stop_event.is_set():
                if proc.poll() is not None:
                    stderr_out = proc.stderr.read().strip() if proc.stderr else ""
                    if stderr_out:
                        with self.lock:
                            self.capture_error = f"tcpdump exited: {stderr_out}"
                    iface_ready = False
                    break

                with self.lock:
                    still_enabled = bool(self.state.get("capture_enabled", True))
                if not still_enabled:
                    break

                if self.rotate_event.is_set():
                    self.rotate_event.clear()
                    break

                if (time.monotonic() - started_mono) >= CSI_ROTATE_SEC:
                    break

                self.stop_event.wait(timeout=0.5)

            self._stop_and_finalize_tcpdump(seg_presence, seg_window)
            self.sync_event.set()

    def _remote_name_prefix(self) -> str:
        return RCLONE_REMOTE.split(":", 1)[0] + ":"

    def _perform_sync(self) -> None:
        if not shutil.which("rclone"):
            with self.lock:
                self.sync_status["rclone_configured"] = False
                self.sync_status["last_sync_ok"] = False
                self.sync_status["last_sync_msg"] = "rclone 未インストール"
            return

        if not Path(RCLONE_CONFIG).exists():
            with self.lock:
                self.sync_status["rclone_configured"] = False
                self.sync_status["last_sync_ok"] = False
                self.sync_status["last_sync_msg"] = "rclone未設定 (rclone config が必要)"
            return

        res_remotes = run_cmd(["rclone", "--config", RCLONE_CONFIG, "listremotes"], timeout=15)
        remotes = [r.strip() for r in res_remotes.stdout.splitlines() if r.strip()]
        expected_remote = self._remote_name_prefix()
        if expected_remote not in remotes:
            with self.lock:
                self.sync_status["rclone_configured"] = False
                self.sync_status["last_sync_ok"] = False
                self.sync_status["last_sync_msg"] = f"リモート '{expected_remote}' 未設定"
            return

        with self.lock:
            self.sync_status["rclone_configured"] = True
            self.sync_status["syncing"] = True

        try:
            res_csv = run_cmd(
                [
                    "rclone",
                    "--config",
                    RCLONE_CONFIG,
                    "copy",
                    str(DATA_DIR),
                    RCLONE_REMOTE,
                    "--include",
                    "*.csv",
                    "--include",
                    "*.jsonl",
                    "--exclude",
                    "*.pcap*",
                ],
                timeout=120,
            )
            if res_csv.returncode != 0:
                raise RuntimeError(f"csv copy failed: {res_csv.stderr.strip()}")

            pending_before = list(DATA_DIR.glob("*/pcap/*.pcap"))
            if pending_before:
                res_move = run_cmd(
                    [
                        "rclone",
                        "--config",
                        RCLONE_CONFIG,
                        "move",
                        str(DATA_DIR),
                        RCLONE_REMOTE,
                        "--include",
                        "*/pcap/*.pcap",
                        "--exclude",
                        "*.pcap.active",
                    ],
                    timeout=300,
                )
                if res_move.returncode != 0:
                    raise RuntimeError(f"pcap move failed: {res_move.stderr.strip()}")
            pending_after = list(DATA_DIR.glob("*/pcap/*.pcap"))
            moved_count = max(0, len(pending_before) - len(pending_after))

            with self.lock:
                self.state["uploaded_count"] = int(self.state.get("uploaded_count", 0)) + moved_count
                self._save_state_locked()
                self.sync_status["uploaded_count"] = self.state["uploaded_count"]
                self.sync_status["last_sync_at"] = iso_now()
                self.sync_status["last_sync_ok"] = True
                self.sync_status["last_sync_msg"] = (
                    f"同期完了 (今回 {moved_count} ファイル転送)" if moved_count else "同期完了 (新規pcapなし)"
                )
        except Exception as exc:
            with self.lock:
                self.sync_status["last_sync_at"] = iso_now()
                self.sync_status["last_sync_ok"] = False
                self.sync_status["last_sync_msg"] = str(exc)[:200]
        finally:
            with self.lock:
                self.sync_status["syncing"] = False

    def sync_loop(self) -> None:
        while not self.stop_event.is_set():
            self._perform_sync()
            self.sync_event.wait(timeout=RCLONE_SYNC_INTERVAL_SEC)
            self.sync_event.clear()

    def update_state(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            changed_label = False
            event_parts: List[str] = []

            if "presence" in payload and payload["presence"] in PRESENCE_VALUES:
                if payload["presence"] != self.state["presence"]:
                    self.state["presence"] = payload["presence"]
                    changed_label = True
                    event_parts.append("presence")

            if "window" in payload and payload["window"] in WINDOW_VALUES:
                if payload["window"] != self.state["window"]:
                    self.state["window"] = payload["window"]
                    changed_label = True
                    event_parts.append("window")

            memo_text = str(payload.get("memo", "")).strip()
            if "memo" in payload and memo_text:
                self.state["memo"] = memo_text
                event_parts.append("memo")
                if not changed_label:
                    self.active_segment_memos.append(memo_text)

            if "capture_enabled" in payload and isinstance(payload["capture_enabled"], bool):
                if payload["capture_enabled"] != self.state["capture_enabled"]:
                    self.state["capture_enabled"] = payload["capture_enabled"]
                    event_parts.append("capture_toggle")
                    self.rotate_event.set()

            if event_parts:
                self.state["updated_at"] = iso_now()
                self.state["epoch_ms"] = int(time.time() * 1000)
                self._save_state_locked()
                self._append_label_records_locked("+".join(event_parts), memo_text)

                if changed_label and self.active_pcap_start is not None:
                    elapsed = (now_local() - self.active_pcap_start).total_seconds()
                    if elapsed >= 3.0:
                        self.rotate_event.set()
                self.sync_event.set()

        return self.get_status()

    def trigger_rotate_and_sync(self) -> Dict[str, Any]:
        self.rotate_event.set()
        self.sync_event.set()
        return self.get_status()

    def _recent_labels(self, limit: int = 15) -> List[Dict[str, Any]]:
        if not LABELS_JSONL.exists():
            return []
        try:
            lines = LABELS_JSONL.read_text(encoding="utf-8").splitlines()
            items = [json.loads(line) for line in lines[-limit:] if line.strip()]
            items.reverse()
            return items
        except Exception:
            return []

    def get_status(self) -> Dict[str, Any]:
        with self.lock:
            active_info = None
            if self.active_pcap_path is not None:
                size = self.active_pcap_path.stat().st_size if self.active_pcap_path.exists() else 0
                try:
                    rel = self.active_pcap_path.relative_to(DATA_DIR).as_posix()
                except ValueError:
                    rel = self.active_pcap_path.name
                active_info = {
                    "filename": self.active_pcap_path.name,
                    "relative_path": rel,
                    "bytes": size,
                    "started_at": (
                        self.active_pcap_start.isoformat(timespec="seconds")
                        if self.active_pcap_start
                        else None
                    ),
                }

            pending_pcaps = sorted(p.name for p in DATA_DIR.glob("*/pcap/*.pcap"))
            usage = shutil.disk_usage(DATA_DIR)
            state_copy = dict(self.state)
            state_copy["label_class"] = make_label_class(state_copy["presence"], state_copy["window"])

            return {
                "state": state_copy,
                "capture": {
                    "status": self.capture_status,
                    "error": self.capture_error,
                    "chanspec": self.last_chanspec_output or CSI_CHANSPEC,
                    "rotate_sec": CSI_ROTATE_SEC,
                    "active_pcap": active_info,
                    "pending_pcap_count": len(pending_pcaps),
                },
                "sync": dict(self.sync_status),
                "disk": {
                    "free_gb": round(usage.free / (1024 ** 3), 2),
                    "total_gb": round(usage.total / (1024 ** 3), 2),
                },
                "recent_labels": self._recent_labels(15),
                "server_time": iso_now(),
            }

    def start(self) -> None:
        with self.lock:
            if not LABELS_CSV.exists():
                self._append_label_records_locked("startup", "logger started")
            self._save_state_locked()

        t_cap = threading.Thread(target=self.capture_loop, name="capture-loop", daemon=True)
        t_sync = threading.Thread(target=self.sync_loop, name="sync-loop", daemon=True)
        t_cap.start()
        t_sync.start()

    def shutdown(self) -> None:
        self.stop_event.set()
        self.rotate_event.set()
        self.sync_event.set()
        with self.lock:
            p = self.state["presence"]
            w = self.state["window"]
        self._stop_and_finalize_tcpdump(p, w)


HTML_PAGE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>CSI 実験ステータス記録</title>
<style>
  :root {
    --bg: #0f172a;
    --card: #1e293b;
    --border: #334155;
    --text: #f8fafc;
    --muted: #94a3b8;
    --green: #16a34a;
    --blue: #2563eb;
    --purple: #7c3aed;
    --gray: #475569;
    --amber: #d97706;
    --red: #dc2626;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 14px;
    background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    line-height: 1.45;
  }
  .wrap { max-width: 640px; margin: 0 auto; }
  header {
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 12px;
  }
  h1 { font-size: 1.15rem; margin: 0; }
  .badge {
    font-size: 0.78rem; padding: 4px 10px; border-radius: 999px;
    background: var(--gray); color: #fff; font-weight: 600;
  }
  .badge.rec { background: var(--green); }
  .badge.err { background: var(--red); }
  .badge.pause { background: var(--amber); }

  .card {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 14px; padding: 14px; margin-bottom: 12px;
  }
  .card h2 {
    font-size: 0.92rem; color: var(--muted);
    margin: 0 0 10px 0; font-weight: 600;
  }
  .btn-grid {
    display: grid; grid-template-columns: 1fr 1fr; gap: 10px;
  }
  button.choice {
    min-height: 68px; border-radius: 12px; border: 2px solid var(--border);
    background: #0f172a; color: var(--text);
    font-size: 1.05rem; font-weight: 700; cursor: pointer;
    padding: 10px; transition: transform 0.05s ease, border-color 0.15s ease;
  }
  button.choice:active { transform: scale(0.98); }
  button.choice.active-in { background: rgba(22,163,74,0.22); border-color: #22c55e; }
  button.choice.active-away { background: rgba(148,163,184,0.20); border-color: #cbd5e1; }
  button.choice.active-open { background: rgba(37,99,235,0.25); border-color: #60a5fa; }
  button.choice.active-closed { background: rgba(124,58,237,0.25); border-color: #a78bfa; }

  .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
  .chip {
    background: #0f172a; border: 1px solid var(--border); color: var(--text);
    border-radius: 999px; padding: 6px 12px; font-size: 0.84rem; cursor: pointer;
  }
  .memo-row { display: flex; gap: 8px; }
  .memo-row input {
    flex: 1; padding: 10px 12px; border-radius: 10px;
    border: 1px solid var(--border); background: #0f172a; color: var(--text);
    font-size: 0.95rem;
  }
  .btn-sm {
    padding: 10px 14px; border-radius: 10px; border: none;
    background: var(--blue); color: #fff; font-weight: 600;
    cursor: pointer; font-size: 0.9rem;
  }
  .btn-sm.secondary { background: var(--gray); }
  .btn-sm.amber { background: var(--amber); }
  .ctrl-row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }

  .kv { font-size: 0.85rem; color: var(--muted); display: grid; gap: 4px; }
  .kv b { color: var(--text); word-break: break-all; }
  table { width: 100%; border-collapse: collapse; font-size: 0.8rem; }
  th, td {
    text-align: left; padding: 6px 4px;
    border-bottom: 1px solid var(--border);
  }
  th { color: var(--muted); font-weight: 600; }
  #toast {
    position: fixed; bottom: 16px; left: 50%; transform: translateX(-50%);
    background: #22c55e; color: #052e16; font-weight: 700;
    padding: 8px 16px; border-radius: 999px; display: none;
    box-shadow: 0 4px 16px rgba(0,0,0,0.4);
  }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>📡 RPi4 CSI 記録ダッシュボード</h1>
    <span id="cap-badge" class="badge">読み込み中...</span>
  </header>

  <div class="card">
    <h2>在室状態（今部屋にいるか）</h2>
    <div class="btn-grid">
      <button id="btn-in" class="choice" onclick="setState({presence:'in_room'})">🟢 部屋にいる<br><small>in_room</small></button>
      <button id="btn-away" class="choice" onclick="setState({presence:'away'})">⚪ 部屋にいない<br><small>away</small></button>
    </div>
  </div>

  <div class="card">
    <h2>窓の開閉状態</h2>
    <div class="btn-grid">
      <button id="btn-open" class="choice" onclick="setState({window:'open'})">🔵 窓：開<br><small>open</small></button>
      <button id="btn-closed" class="choice" onclick="setState({window:'closed'})">🟣 窓：閉<br><small>closed</small></button>
    </div>
  </div>

  <div class="card">
    <h2>メモ / イベント記録</h2>
    <div class="chips">
      <button class="chip" onclick="sendMemo('入室')">入室</button>
      <button class="chip" onclick="sendMemo('退室')">退室</button>
      <button class="chip" onclick="sendMemo('窓を開けた')">窓を開けた</button>
      <button class="chip" onclick="sendMemo('窓を閉めた')">窓を閉めた</button>
      <button class="chip" onclick="sendMemo('静止中')">静止中</button>
      <button class="chip" onclick="sendMemo('歩行・移動')">歩行・移動</button>
    </div>
    <div class="memo-row">
      <input id="memo-input" type="text" placeholder="自由メモを入力..." />
      <button class="btn-sm" onclick="submitMemoInput()">記録</button>
    </div>
  </div>

  <div class="card">
    <h2>CSI キャプチャ &amp; Google Drive 同期</h2>
    <div class="kv">
      <div>現在のクラス / 更新時刻: <b id="st-class">-</b> (<b id="st-updated">-</b>)</div>
      <div>Chanspec: <b id="st-chan">-</b></div>
      <div>記録中ファイル: <b id="st-active">-</b></div>
      <div>Google Drive: <b id="st-sync">-</b></div>
      <div>Pi 未転送pcap: <b id="st-pending">0</b> 件 / 転送済累計: <b id="st-uploaded">0</b> 件 / 空き容量: <b id="st-free">-</b> GB</div>
    </div>
    <div class="ctrl-row">
      <button id="btn-toggle-cap" class="btn-sm amber" onclick="toggleCapture()">⏸ 一時停止</button>
      <button class="btn-sm secondary" onclick="syncNow()">🔄 今すぐ区切ってDrive同期</button>
    </div>
  </div>

  <div class="card">
    <h2>最近の記録履歴（最新15件）</h2>
    <table>
      <thead>
        <tr><th>時刻</th><th>在室</th><th>窓</th><th>メモ / イベント</th></tr>
      </thead>
      <tbody id="hist-body"></tbody>
    </table>
  </div>
</div>
<div id="toast">保存しました</div>

<script>
let currentCapEnabled = true;

function showToast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.style.display = 'block';
  setTimeout(() => { el.style.display = 'none'; }, 1800);
}

function fmtBytes(b) {
  if (!b) return '0 B';
  if (b < 1024) return b + ' B';
  if (b < 1024*1024) return (b/1024).toFixed(1) + ' KB';
  return (b/(1024*1024)).toFixed(2) + ' MB';
}

function render(data) {
  const s = data.state;
  const c = data.capture;
  const sy = data.sync;
  currentCapEnabled = !!s.capture_enabled;

  document.getElementById('btn-in').className = 'choice ' + (s.presence === 'in_room' ? 'active-in' : '');
  document.getElementById('btn-away').className = 'choice ' + (s.presence === 'away' ? 'active-away' : '');
  document.getElementById('btn-open').className = 'choice ' + (s.window === 'open' ? 'active-open' : '');
  document.getElementById('btn-closed').className = 'choice ' + (s.window === 'closed' ? 'active-closed' : '');

  const badge = document.getElementById('cap-badge');
  if (c.status === 'recording') {
    badge.className = 'badge rec'; badge.textContent = '● 記録中';
  } else if (c.status === 'paused') {
    badge.className = 'badge pause'; badge.textContent = '⏸ 一時停止';
  } else {
    badge.className = 'badge err'; badge.textContent = '⚠ ' + c.status;
  }

  document.getElementById('btn-toggle-cap').textContent = currentCapEnabled ? '⏸ 一時停止' : '▶ 記録再開';
  document.getElementById('st-class').textContent = s.label_class || '-';
  document.getElementById('st-updated').textContent = s.updated_at || '-';
  document.getElementById('st-chan').textContent = c.chanspec || '-';
  document.getElementById('st-active').textContent = c.active_pcap
    ? `${c.active_pcap.relative_path || c.active_pcap.filename} (${fmtBytes(c.active_pcap.bytes)})`
    : (c.error || 'なし');

  const syncText = sy.rclone_configured
    ? `${sy.last_sync_msg || '-'} (${sy.last_sync_at || '-'})`
    : `未連携 (${sy.last_sync_msg || 'rclone config 待ち'})`;
  document.getElementById('st-sync').textContent = syncText;
  document.getElementById('st-pending').textContent = c.pending_pcap_count;
  document.getElementById('st-uploaded').textContent = sy.uploaded_count;
  document.getElementById('st-free').textContent = data.disk.free_gb;

  const tbody = document.getElementById('hist-body');
  tbody.innerHTML = '';
  for (const row of data.recent_labels) {
    const tr = document.createElement('tr');
    const t = (row.timestamp_iso || '').replace('T', ' ').slice(5, 19);
    const p = row.presence === 'in_room' ? '🟢 在室' : '⚪ 不在';
    const w = row.window === 'open' ? '🔵 開' : '🟣 閉';
    const m = row.memo ? row.memo : `(${row.event_type})`;
    tr.innerHTML = `<td>${t}</td><td>${p}</td><td>${w}</td><td></td>`;
    tr.children[3].textContent = m;
    tbody.appendChild(tr);
  }
}

async function refresh() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    render(data);
  } catch (e) {}
}

async function setState(patch) {
  const res = await fetch('/api/update', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(patch)
  });
  const data = await res.json();
  render(data);
  showToast('記録しました');
}

function sendMemo(text) {
  setState({memo: text});
}

function submitMemoInput() {
  const inp = document.getElementById('memo-input');
  const v = inp.value.trim();
  if (!v) return;
  inp.value = '';
  setState({memo: v});
}

function toggleCapture() {
  setState({capture_enabled: !currentCapEnabled});
}

async function syncNow() {
  const res = await fetch('/api/sync', {method: 'POST'});
  const data = await res.json();
  render(data);
  showToast('ファイル分割・同期を要求しました');
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


def make_handler(service: CSILoggerService):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(self, obj: Dict[str, Any], code: int = HTTPStatus.OK) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/" or self.path.startswith("/?"):
                self._send_html(HTML_PAGE)
            elif self.path == "/api/status":
                self._send_json(service.get_status())
            else:
                self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length > 0 else b"{}"
            if self.path == "/api/update":
                try:
                    payload = json.loads(raw.decode("utf-8"))
                    if not isinstance(payload, dict):
                        raise ValueError("invalid payload")
                except Exception:
                    self._send_json({"error": "invalid json"}, HTTPStatus.BAD_REQUEST)
                    return
                self._send_json(service.update_state(payload))
            elif self.path == "/api/sync":
                self._send_json(service.trigger_rotate_and_sync())
            else:
                self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    return Handler


def main() -> None:
    service = CSILoggerService()
    service.start()
    server = ThreadingHTTPServer(("0.0.0.0", CSI_PORT), make_handler(service))

    def handle_sig(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)

    try:
        server.serve_forever()
    finally:
        service.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
