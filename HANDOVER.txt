# 引き継ぎ書：Raspberry Pi 4B（Nexmon CSI 自動収集・Google Drive 5TB・Colab 解析）

更新日時: 2026-11-25 17:30 (JST)
GitHub リポジトリ: `https://github.com/hann451/rpi4-nexmon-csi-setup`
Git ユーザー設定: `hann451` / `285997610+hann451@users.noreply.github.com`（※個人 Gmail を公開しないため noreply アドレスを使用）

---

## 1. プロジェクト概要・現在の到達点

- **目的**: カメラや専用センサーを使わず、Raspberry Pi 4B（USB ブート）で取得した 5GHz Wi-Fi CSI（Channel State Information）のゆらぎから、部屋の **在室状態（`in_room` / `away`）** と **窓の開閉（`open` / `closed`）** の 4 クラスを AI で推定する実験環境を構築・解析すること。
- **現在の状態（フェーズ 4：データ解析・AI 学習フェーズ）**:
  - ✅ **装置構築・再起動永続化**: 完了＆24時間稼働中
  - ✅ **スマホ Web UI（Tailscale 経由）・Google Drive 自動整理**: 完了＆24時間稼働中
  - ✅ **GitHub 公開・図解教材整備**: 完了
  - 👉 **いまここ**: Google Colab でのデータ可視化・全件変換・日またぎ検証・GPU (PyTorch) 学習

---

## 2. 稼働中のハードウェア・ネットワーク・クラウド情報

### Raspberry Pi 4B（24時間自律稼働中）
- **OS / カーネル**: Raspberry Pi OS Bullseye 32bit armhf / `5.10.92-v7l+`（USB 3.0 ブート、microSD なし）
- **固定ロック済みパッケージ (`apt-mark showhold`)**: `raspberrypi-kernel`, `raspberrypi-kernel-headers` (`1:1.20220120-1`), `firmware-brcm80211`（※`apt upgrade` は実行しないこと）
- **Nexmon CSI**: `~/nexmon/patches/bcm43455c0/7_45_189/nexmon_csi` でビルド、`brcmfmac43455-sdio.bin` および `/lib/modules/5.10.92-v7l+/.../brcmfmac.ko` を永続化済み（`depmod -a` 済み）
- **ネットワーク**:
  - ホスト名: `raspberrypi.local`
  - 有線 LAN (`eth0`): `192.168.10.2/24`（`/etc/dhcpcd.conf` に固定設定、GW `192.168.10.1`、DNS `192.168.10.1 8.8.8.8`）
  - Wi-Fi (`wlan0` / `mon0`): `/etc/dhcpcd.conf` で `denyinterfaces wlan0 mon0` 設定済み（CSI モニターモード専用）
  - **Tailscale**: `iptables-legacy` / `ip6tables-legacy` 適用済み、Tailscale IP **`100.68.123.109`**
  - **SSH 接続**: `ssh pi@100.68.123.109`（Tailscale 経由・外出先含む）または `ssh pi@raspberrypi.local`（同一 LAN 内）。初期パスワードは変更済み。
- **常駐サービス (`systemd`)**: `csi-logger.service`（実体 `/opt/csi-logger/app.py`）
  - 起動時に自動で `wlan0` / `mon0` を Ch 36 / 80MHz (`0xe02a`) に設定し、5分ごと＆ラベル変更時に `.pcap` を分割保存
  - スマホ記録 Web UI: **`http://100.68.123.109:8080`**（または `http://raspberrypi.local:8080`）
  - `rclone` (`gdrive:rpi4_nexmon_csi`) で 60 秒ごとに CSV を `copy`、完了 `.pcap` を `move`（転送済み `.pcap` は Pi から自動削除、空き 2GB 未満で安全停止）

### Google Drive（5TB）および Google Colab
- **Drive ルートフォルダ**: `マイドライブ/rpi4_nexmon_csi`（Folder ID: `1evt-u4cwyXL0ij2xm_5oL0uhNfKDO1XI`）
- **フォルダ構造**:
  - 最上位マスター: `labels.csv`, `labels.jsonl`, `segments.csv`
  - 日付別フォルダ: `YYYY-MM-DD/pcap/csi_YYYYMMDD_HHMMSS_<presence>_<window>.pcap`, `YYYY-MM-DD/labels_YYYY-MM-DD.csv`, `YYYY-MM-DD/segments_YYYY-MM-DD.csv`
  - 前処理キャッシュ: `processed/<segment_id>.npz`（Colab 増分変換の出力先）
  - ノートブック: `notebooks/csi_analysis_colab.ipynb`（Folder ID: `1hC64pEccOyzZNhE69VvjR8lKxICQLIjd`）
- **Colab ノートブック直接 URL**: `https://colab.research.google.com/drive/1-Qblc93YR1oZcv0Hjz3Vj5Uk8HUkdfBD`（File ID: `1-Qblc93YR1oZcv0Hjz3Vj5Uk8HUkdfBD`）
- **2026-11-25 時点のデータ蓄積・検証結果**:
  - 累計 **461 ファイル / 13.54 GB / 38.01 時間**（`away__closed`: 12.84h, `in_room__closed`: 9.73h, `in_room__open`: 9.59h, `away__open`: 5.85h）
  - バイナリ検証済み：全パケット `magic=0x1111`・`chanspec=0xe02a`・約 92.5 pkt/s
  - 主要送信元 MAC:
    - `ec:5a:31:a6:01:ce`（中継器 `Extender-5G-01C0`、平均 `-48.1 dBm`、約 9.4 pkt/s）
    - `98:f1:99:a4:02:0f`（親ルーター 5GHz、平均 `-66.9 dBm`、約 27.3 pkt/s）
  - 有効サブキャリア帯域: Ch 36 20MHz 制御チャネル＝生 FFT インデックス **`134..191`**（うち `160` は DC ヌル、`187..191` はガードバンド）
  - Colab ベースライン結果: 40 ファイル（10秒窓 1,160 サンプル）で RandomForest ファイル分割テスト **accuracy 98%**（ただし同日の近接セグメントを含むため、今後は日またぎ評価が必須）

---

## 3. リポジトリ構成

- `README.md`: 全セットアップ手順・CSV 連携方法・教材リンク
- `docs/beginner_guide.html`: 専門用語ゼロから理解できる全体図＆用語辞典（19語・検索機能付き）
- `docs/guide.html`: 技術詳細・1042B パケット解剖・5つの難所解説・インタラクティブ CSI シミュレータ
- `notebooks/csi_analysis_colab.ipynb`: Google Colab 解析・増分 `.npz` 変換・可視化・ベースライン分類ノートブック
- `csi-logger/app.py`, `csi-logger/csi-logger.service`, `csi-logger/install.sh`: ラズパイ常駐システム一式
- `scripts/start_csi.sh`: 手動 CSI 開始スクリプト
- `boot/ssh`, `boot/wpa_supplicant.conf.example`, `.gitignore`
- `HANDOVER.md` / `HANDOVER.txt`: 本引き継ぎ書

---

## 4. Linux OS への引き継ぎ・環境構築手順

データ本体はすべて Google Drive、コードとドキュメントはすべて GitHub、測定装置は Raspberry Pi 上で自律稼働しているため、**Windows から Linux へ手動コピーが必要なファイルはありません**。Linux 側で以下を行うだけで続きから再開できます。

### (1) GitHub リポジトリをクローンして Git ユーザーを設定する
```bash
git clone https://github.com/hann451/rpi4-nexmon-csi-setup.git
cd rpi4-nexmon-csi-setup
git config user.name "hann451"
git config user.email "285997610+hann451@users.noreply.github.com"
```
（※非公開リポジトリの場合、認証には `gh auth login` または Personal Access Token / SSH 鍵を使用）

### (2) Tailscale を Linux にも導入して Raspberry Pi へ接続できるようにする
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```
同じアカウントでログイン後、以下で Pi へ SSH 接続できることを確認：
```bash
ssh pi@100.68.123.109
```
（※同一 LAN 内なら `ssh pi@raspberrypi.local` も可）

### (3) Google Colab / Google Drive を開く
Linux のブラウザで同じ Google アカウント（5TB / Google AI Pro）にログインし、以下の URL を開くだけで続きから実行可能：
- `https://colab.research.google.com/drive/1-Qblc93YR1oZcv0Hjz3Vj5Uk8HUkdfBD`

### (4) Linux 側の AI エージェント（Antigravity 等）へ引き継ぐとき
クローンした `rpi4-nexmon-csi-setup` フォルダを開き、**「`HANDOVER.md` を読み込んで続きの作業を進めてください」** と指示する。

---

## 5. Linux 移行後の残タスク（Next Steps）

1. **実測ヒートマップの比較セル追加**: Colab ノートブックに「不在 (`away`)」と「在室 (`in_room`)」の実測ヒートマップを上下に並べて見比べるセルを追加して波形の違いを確認する。
2. **全ファイル増分変換 & 日またぎ評価（リーク検証）**:
   - `MAX_FILES_THIS_RUN = None` にして全セグメントを `processed/*.npz` へ変換する。
   - 2026-11-24 のデータで学習 → 2026-11-25 以降のデータでテストする「日またぎ評価」を行い、本当の実力（汎化性能）を確認する。
3. **Colab GPU（L4 / T4 / A100）を使った本格モデル構築**:
   - 中継器 (`ext_amp`) ＋ 親ルーター (`rtr_amp`) の 2 方向 CSI を組み合わせた PyTorch 1D/2D CNN モデルで 4 クラス分類精度を高める。
4. **（任意）完成版 USB イメージのバックアップ**:
   - Linux では Pi 停止後に USB メモリを挿して `lsblk` でデバイス名を確認し、`sudo dd if=/dev/sdX of=raspios-bullseye-nexmon-csi.img bs=4M status=progress` でフルバックアップ取得可能（※出力先 `of=` と入力元 `if=` を絶対に逆にしないこと。機密情報を含むため個人保管）。
