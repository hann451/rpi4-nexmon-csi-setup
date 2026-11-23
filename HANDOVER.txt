# 引き継ぎ書：Raspberry Pi 4B（USBブート / Nexmon CSI用）セットアップ

更新日時: 2026-11-23 22:08 (JST)
作業フォルダ: `C:\Users\daiyu\.gemini\antigravity\scratch\rpi4-nexmon-csi-setup`

---

## 1. プロジェクト概要・目的

- **目的**: Raspberry Pi 4B（USBブート）を用いた Wi-Fi CSI 取得実験（Nexmon CSI）環境の構築と、設定手順の再現用リポジトリ整備。
- **対象デバイス**: Raspberry Pi 4B（USB 3.0 青いポートからUSBブート、microSDカードは取り外し済み）
- **OSイメージ**: `2022-01-28-raspios-bullseye-armhf.img`（32bit armhf / Debian Bullseye）
- **維持すべきカーネル**: `5.10.92-v7l+`（Nexmon CSI 動作のため必須）

---

## 2. これまでに完了した作業と検証結果

### (1) USBブート領域（Windows側 `D:\` / ラベル `boot`）の初期設定（完了）
- `D:\ssh`: 0バイト・拡張子なしの空ファイルを作成し、初回SSHを有効化済み。
- `D:\wpa_supplicant.conf`: UTF-8（BOMなし）・LF改行（`0x0A`のみ）で作成済み（`152`バイト）。

### (2) 初回起動・有線LANでのSSH接続・初期パスワード変更（完了）
- **ホスト名**: `raspberrypi.local`
- **有線LAN IPアドレス**: `192.168.10.2`（MACアドレス: `2c:cf:67:0d:34:da`）
- **作業PC側ネットワーク**: `192.168.10.0/24`（ルーター/ゲートウェイ: `192.168.10.1`, 作業PC: `192.168.10.8`）
- **SSH接続コマンド**: `ssh pi@raspberrypi.local` または `ssh pi@192.168.10.2`
- **ログインユーザー**: `pi`（`passwd` により初期パスワード `raspberry` から変更済み）

### (3) カーネル・ヘッダ・ファームウェアの固定（完了）
Pi上で以下を導入・固定済み：
- `uname -r` → `5.10.92-v7l+`
- `raspberrypi-kernel-headers_1.20220120-1_armhf.deb` を pool から直接取得し `dpkg -i` で導入済み（`/lib/modules/5.10.92-v7l+/build` 確認済み）
- `apt-mark showhold` → `firmware-brcm80211`, `raspberrypi-kernel`, `raspberrypi-kernel-headers` の3パッケージを hold 済み

> **重要**: `sudo apt upgrade` / `sudo apt full-upgrade` は Nexmon 環境への影響を避けるため実行しないこと。

### (4) Wi-Fi（`wlan0`）調査と有線LAN運用の決定（完了）
- 周囲の 5GHz AP として Channel 36 (`Extender-5G-01C0` 等) を確認済み。
- Nexmon CSI 実験時は内蔵 Wi-Fi（`wlan0`）をモニターモードに切り替えるため、**SSH 等は有線LAN（`eth0` / `192.168.10.2`）で接続する方針**で確定。

### (5) ローカル Git リポジトリ整備（完了）
- Windows PC 側に Git (`2.56.1.windows.1`) をインストール済み。
- 作業フォルダで `git init -b main`、ローカルリポジトリ用の `user.name` (`hann451`) / `user.email` (`285997610+hann451@users.noreply.github.com`) を設定し、コミット済み。

### (6) Nexmon CSI ビルド・永続化・実パケット受信確認（完了）
Pi 上で以下をすべて実施・検証済み：
- **ビルド依存解決**: `libisl.so.10 -> libisl.so.23.0.0`, `libmpfr.so.4 -> libmpfr.so.6.1.0` のリンク作成、`python2` 導入および `~/nexmon/buildtools/` 内スクリプトの shebang 修正（`python` → `python2`）。
- **Nexmon 本体 + `nexmon_csi` ビルド**: `~/nexmon/patches/bcm43455c0/7_45_189/nexmon_csi` で `make install-firmware` 成功。
  - `dmesg` にて `Firmware: BCM4345/6 wl0: Nov 23 2026 12:52:19 version 7.45.189 (nexmon.org/csi: 1232-2)` のロードを確認。
- **ユーティリティ導入**: `/usr/bin/nexutil` および `/usr/local/bin/makecsiparams`、`tcpdump` を導入済み。
- **永続化設定**:
  - 元のファームウェア・ドライバを `~/brcmfmac43455-sdio.bin.orig` と `~/brcmfmac.ko.orig` にバックアップ済み。
  - Nexmon 版 `brcmfmac.ko` を `/lib/modules/5.10.92-v7l+/kernel/drivers/net/wireless/broadcom/brcm80211/brcmfmac/brcmfmac.ko` へコピーし、`sudo depmod -a` 実行済み。
  - `/etc/dhcpcd.conf` に `denyinterfaces wlan0 mon0` を追記し、`wpa_supplicant` 停止後に `wlan0` が落ちないよう対策済み。
- **実機 CSI パケット受信確認**:
  - `makecsiparams -c 36/80 -C 1 -N 1` (`KuABEQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==`) を `nexutil` で適用し、`nexutil -Iwlan0 -k` で `chanspec: 0xe02a, 36/80` を確認。
  - `mon0` を起動し、`sudo tcpdump -i wlan0 dst port 5500 -vv -c 5` にて `10.10.10.10.5500 > 255.255.255.255.5500: [no cksum] UDP, length 1042` の CSI パケット 5 個の受信に成功（5 packets captured / 0 packets dropped by kernel）。

---

## 3. ローカルリポジトリ構成

場所: `C:\Users\daiyu\.gemini\antigravity\scratch\rpi4-nexmon-csi-setup`

- `README.md`: USBブート・SSH・カーネル固定・Nexmon CSI ビルド/永続化/キャプチャ・常時記録システムの検証済み手順書
- `docs/guide.html`: 仕組み・1042Bパケット構造・難所解説・全体構成のインタラクティブ図解教材
- `csi-logger/app.py`: 常時CSI記録（5分ローテ）・Google Drive (`rclone`) 自動アップロード・スマホ用ラベル記録 Web UI（`8080`番ポート）
- `csi-logger/csi-logger.service`: `systemd` 常駐サービス定義
- `csi-logger/install.sh`: `Asia/Tokyo` 設定、`rclone`・`tailscale` 導入、`iptables-legacy` 切替、サービス有効化スクリプト
- `scripts/start_csi.sh`: 手動で `wlan0` / `mon0` の CSI キャプチャを開始するヘルパースクリプト
- `boot/ssh`: SSH有効化用空ファイル
- `boot/wpa_supplicant.conf.example`: Wi-Fi設定テンプレート（`<SSID>`, `<PASSWORD>` のみで機密情報なし）
- `.gitignore`: `*.img`, `*.img.gz`, `*.zip`, `*.xz`, `wpa_supplicant.conf` を除外
- `HANDOVER.md` / `HANDOVER.txt`: 本引き継ぎ書

※ リポジトリ内に Wi-Fi パスワードや OS イメージ、rclone/Tailscale トークンなどの機密・大容量データは含まれていない。

---

## 4. PC側ツール環境の確認状況（2026-11-23時点）

- **Google Drive (`G:\`)**: 空き容量 `162 GB`（バックアップ保存先として利用予定。アカウント自体は 5TB）
- **Win32DiskImager**: `C:\Program Files (x86)\ImageWriter\Win32DiskImager.exe` 存在確認済み
- **Git (`git`)**: `2.56.1.windows.1` インストール済み（`C:\Program Files\Git\cmd\git.exe`）

---

## 5. 本番稼働済み構成と残タスク

### 完了済み：本番自動記録・Tailscale・Google Drive 連携
- **再起動永続化**: `sudo reboot` 後も `csi-logger.service` と Nexmon CSI ドライバが自動復帰し、`~/csi_data/pcap/` への分割記録が継続されることを確認済み。
- **有線 LAN 固定 IP & Tailscale**: `/etc/dhcpcd.conf` に `eth0` (`192.168.10.2/24`, GW `192.168.10.1`) を固定し、`iptables-legacy` へ切り替えたことで Tailscale (`100.68.123.109`) オンライン化を確認済み。
- **スマホ Web UI**: `http://100.68.123.109:8080`（Tailscale）および `http://raspberrypi.local:8080` で表示・記録確認済み。
- **Google Drive (`gdrive:rpi4_nexmon_csi`)**: SSH ポート転送（`-L 53682:127.0.0.1:53682`）経由で `rclone config` 完了。`labels` と `pcap` ディレクトリ作成・自動アップロードおよび転送済み `.pcap` の Pi 側自動削除を確認済み。

### 残タスク A: GitHub へ非公開リポジトリ（Private）として Publish
1. 作業フォルダ `C:\Users\daiyu\.gemini\antigravity\scratch\rpi4-nexmon-csi-setup` を VS Code で開く。
2. ソース管理（Git）から **Publish Branch** → **Publish to GitHub private repository** を選択し、`rpi4-nexmon-csi-setup` を公開する（公開リポジトリにしても機密情報は含まれません）。

### 残タスク B: 完成版 USB イメージのバックアップ（Google Drive `G:\`）
必要になったタイミングで、完成版イメージを `G:\` へ保存する（`Win32DiskImager` の **Read**、「Read Only Allocated Partitions」はオフ、**Write は絶対押さない**）。
※ イメージには Wi-Fi パスワード・SSH ホスト鍵・Tailscale/rclone トークンが含まれるため、共有リンクは作らず個人保管とすること。
