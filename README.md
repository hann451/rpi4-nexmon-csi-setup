# Raspberry Pi 4B セットアップ手順（Nexmon CSI 用・USBブート）

Wi-Fi CSI 取得実験（Nexmon CSI）のため、カーネル 5.10 系を維持した Raspberry Pi OS (Bullseye) のセットアップ〜24時間自動記録〜Google Drive (5TB) 整理〜Google Colab 解析までの一式です。

### まずはこちら（図解教材 ＆ Colab ノートブック）
- 🌱 **超やさしい全体図＆用語辞典**: [`docs/beginner_guide.html`](docs/beginner_guide.html)（専門用語ゼロから全体構成・登場人物・19用語を図解）
- 🔬 **技術詳細・パケット解剖・難所解説**: [`docs/guide.html`](docs/guide.html)（CSI シミュレータ・1042B UDP パケット解説）
- 💻 **Google Colab 解析・AI 学習ノートブック**: [`notebooks/csi_analysis_colab.ipynb`](notebooks/csi_analysis_colab.ipynb)（Drive 直結・増分 `.npz` 変換・4クラス可視化・ベースライン分類）

> このリポジトリには OS イメージ・パスワード・Wi-Fi 情報を含めません。

## 前提

- 本体: Raspberry Pi 4B（USBブート）
- OS: `2022-01-28-raspios-bullseye-armhf.img`（Raspberry Pi 公式の旧版アーカイブから入手）
- 初期ユーザー: `pi` / 初期パスワード: `raspberry`（初回ログイン後すぐ変更すること）
- カーネル: `5.10.92-v7l+`（Nexmon CSI 対応のため固定）

## 1. boot 領域への設定ファイル追加（Windows）

OS を書き込んだ USB メモリの `boot` ドライブ直下に以下を置く。
`boot` ドライブ = ボリュームラベルが `boot` で、直下に `config.txt` と `cmdline.txt` があるリムーバブルドライブ。

- Windows が「フォーマットが必要」と表示したら必ず **キャンセル** する（Linux 領域の正常な警告）。
- `boot/ssh`: SSH 有効化用の **空ファイル**（拡張子なし。`ssh.txt` にしない）
- Wi-Fi を使う場合のみ `boot/wpa_supplicant.conf`（`boot/wpa_supplicant.conf.example` を参考に作成。UTF-8 BOM なし・改行 LF）

注意:
- SSID は正確に入力する（例: `5G` の後ろのハイフンなど、綴りの違いで接続できない）。
- WPA3 専用の SSID は旧 OS では接続できないことがある。WPA2 または WPA2/WPA3 混在の SSID を使う。
- `wpa_supplicant.conf` はパスワードを含むので Git に入れない（`.gitignore` 済み）。

## 2. 起動

1. USB メモリを「安全な取り外し」で抜く
2. Pi の電源を切り、microSD を抜く
3. USB メモリを Pi の青いポート（USB 3.0）に挿す
4. LAN ケーブル（使う場合）と電源を接続し、1〜2 分待つ（初回は自動で 1 回再起動する）

## 3. SSH 接続

```
ssh pi@raspberrypi.local
```

- 名前解決できない場合: `arp -a` やルーターの端末一覧から Pi の IP を探す
  （MAC 先頭 `b8:27:eb` / `dc:a6:32` / `e4:5f:01` / `2c:cf:67` など）
- 以前の環境とホスト鍵が衝突した場合: `ssh-keygen -R raspberrypi.local`

## 4. ログイン後に最初にやること

```
uname -r                                                          # 5.10.92-v7l+ であること
sudo apt-mark hold raspberrypi-kernel raspberrypi-kernel-headers  # カーネル固定（最重要）
apt-mark showhold                                                 # 2 つ表示されること
passwd                                                            # 初期パスワードを変更
```

固定が済むまで `sudo apt upgrade` / `full-upgrade` は実行しない。

## 5. Nexmon CSI のビルドとインストール（検証済み手順）

カーネル `5.10.92-v7l+` 上で実機検証済みの手順です（Raspberry Pi OS Bullseye 32bit armhf）。

### 5-1. 依存パッケージのインストール

`raspberrypi-kernel-headers` は `apt` リポジトリ側の最新版（`1:1.20230405-1`）とカーネルバージョンが一致しないため、ここでは含めません。

```bash
sudo apt update
sudo apt install -y git libgmp3-dev gawk qpdf bc flex bison libssl-dev make libtool-bin automake autoconf texinfo python2 tcpdump
```

### 5-2. カーネル一致ヘッダ（`1:1.20220120-1`）の個別導入

`5.10.92-v7l+` に対応するヘッダ `.deb` を直接取得してインストールし、再度 `hold` します（USB ストレージ環境では展開に 10〜25 分程度かかります）。

```bash
cd ~
wget http://archive.raspberrypi.org/debian/pool/main/r/raspberrypi-firmware/raspberrypi-kernel-headers_1.20220120-1_armhf.deb
sudo dpkg -i raspberrypi-kernel-headers_1.20220120-1_armhf.deb
sudo apt-mark hold raspberrypi-kernel raspberrypi-kernel-headers
ls /lib/modules/5.10.92-v7l+/build > /dev/null && echo "headers OK"
```

### 5-3. 共有ライブラリのシンボリックリンク作成

Nexmon のビルドツールが参照する古いライブラリ名へリンクを張ります。

```bash
sudo ln -sf /usr/lib/arm-linux-gnueabihf/libisl.so.23.0.0 /usr/lib/arm-linux-gnueabihf/libisl.so.10
sudo ln -sf /usr/lib/arm-linux-gnueabihf/libmpfr.so.6.1.0 /usr/lib/arm-linux-gnueabihf/libmpfr.so.4
```

### 5-4. Nexmon 本体の取得・Python 2 対応・ビルド

Bullseye では `/usr/bin/python` が Python 3 を指すため、`buildtools/` 内のスクリプトの shebang を `python2` に書き換えてからビルドします。

```bash
cd ~
git clone https://github.com/seemoo-lab/nexmon.git
grep -rl '^#!/usr/bin/env python$' ~/nexmon/buildtools/ | xargs sed -i 's|^#!/usr/bin/env python$|#!/usr/bin/env python2|'
cd ~/nexmon
source setup_env.sh
make
```

### 5-5. 元の Wi-Fi ファームウェアのバックアップ

```bash
sudo cp -n /lib/firmware/brcm/brcmfmac43455-sdio.bin ~/brcmfmac43455-sdio.bin.orig
```

### 5-6. `nexmon_csi` のビルドとファームウェア適用

対象ファームウェア版ディレクトリ `7_45_189` 配下でクローンしてビルドします（新しいシェルを開いた場合は再度 `source ~/nexmon/setup_env.sh` を実行してください）。

```bash
cd ~/nexmon/patches/bcm43455c0/7_45_189/
git clone https://github.com/seemoo-lab/nexmon_csi.git
cd nexmon_csi
make install-firmware
```

### 5-7. `nexutil` と `makecsiparams` のビルド・インストール

```bash
cd ~/nexmon/utilities/nexutil
make && sudo make install

cd ~/nexmon/patches/bcm43455c0/7_45_189/nexmon_csi/utils/makecsiparams
make && sudo cp makecsiparams /usr/local/bin/
```

### 5-8. 変更版ドライバ `brcmfmac.ko` の永続化と `firmware-brcm80211` のロック

`make install-firmware` は一時的な `insmod` のみ行うため、再起動後も Nexmon 版ドライバが読み込まれるように `/lib/modules` へ配置し、ファームウェアパッケージも `hold` します。

```bash
KO_PATH=$(modinfo -n brcmfmac)
sudo cp -n "$KO_PATH" ~/brcmfmac.ko.orig
sudo cp ~/nexmon/patches/bcm43455c0/7_45_189/nexmon_csi/brcmfmac_5.10.y-nexmon/brcmfmac.ko "$KO_PATH"
sudo depmod -a
sudo apt-mark hold firmware-brcm80211
apt-mark showhold
```

### 5-9. `dhcpcd` / `wpa_supplicant` による `wlan0` リセットの抑止

`wpa_supplicant` 停止直後に `dhcpcd` が `wlan0` を DOWN にするのを防ぎます。

```bash
grep -q "^denyinterfaces wlan0" /etc/dhcpcd.conf || echo "denyinterfaces wlan0 mon0" | sudo tee -a /etc/dhcpcd.conf
```

## 6. CSI キャプチャ実行手順

- CSI 取得時は内蔵 Wi-Fi（`wlan0`）をモニターモードにするため、Wi-Fi 経由の SSH は切れる。実験時は有線 LAN などの別経路を使う。
- ルーターの IP 固定は不要。測定チャンネル・帯域幅を送信側と合わせる（ルーターのチャンネル固定があると安定）。

### 手動で CSI キャプチャを開始する例（Channel 36 / 80MHz）

```bash
sudo systemctl stop wpa_supplicant 2>/dev/null || true
sudo pkill wpa_supplicant 2>/dev/null || true
sleep 1

CSIPARAMS=$(makecsiparams -c 36/80 -C 1 -N 1)
sudo ip link set wlan0 up
sudo nexutil -Iwlan0 -s500 -b -l34 -v"$CSIPARAMS"
sudo iw phy $(iw dev wlan0 info | gawk '/wiphy/ {printf "phy" $2}') interface add mon0 type monitor 2>/dev/null || true
sudo ip link set mon0 up
sudo nexutil -Iwlan0 -k

# 疎通確認（UDP 5500 番ポート宛のパケットが wlan0 に流れる）
sudo tcpdump -i wlan0 dst port 5500 -vv -c 5

# pcap ファイルへ保存する場合
sudo tcpdump -i wlan0 dst port 5500 -w csi_capture.pcap
```

本リポジトリの [`scripts/start_csi.sh`](scripts/start_csi.sh) を Pi 上にコピーして実行することもできます：

```bash
bash start_csi.sh 36/80 1 1
```

## 7. 常時CSI記録・スマホ用ラベル記録Web UI・Google Drive自動同期・Tailscale設定

再起動後も自動で CSI キャプチャ（5分ごとに `.pcap` 分割保存）・Google Drive（`rclone`）自動アップロード・スマホから操作できる在室/窓開閉ラベル記録 Web UI を起動する構成です。

### 構成ファイル（[`csi-logger/`](csi-logger/) / [`docs/guide.html`](docs/guide.html)）
- [`docs/guide.html`](docs/guide.html): 仕組み・1042バイトUDPパケット構造・難所解説・全体構成のインタラクティブ図解教材
- [`csi-logger/app.py`](csi-logger/app.py): 常時起動デーモン（`wlan0`/`mon0` 自動復旧、`tcpdump` 5分ローテーション、ラベル CSV/JSONL 保存、`rclone move` による Google Drive 自動同期、ポート `8080` Web UI）
- [`csi-logger/csi-logger.service`](csi-logger/csi-logger.service): `systemd` 自動起動ユニット
- [`csi-logger/install.sh`](csi-logger/install.sh): `Asia/Tokyo` タイムゾーン設定、`rclone`・`tailscale` 導入、`iptables-legacy` 切替、サービス登録スクリプト

### セットアップ手順

1. **Windows PC から Pi へファイルを転送する**（PowerShell でリポジトリフォルダから実行）:
   ```powershell
   scp -r csi-logger scripts pi@raspberrypi.local:~/
   ```
2. **有線 LAN (`eth0`) の固定 IP を設定する**（再起動時の DHCP タイムアウトによる `169.254.x.x` 化・ネット不通を防ぐため推奨）:
   ```bash
   grep -q "^interface eth0" /etc/dhcpcd.conf || cat << 'EOF' | sudo tee -a /etc/dhcpcd.conf

   interface eth0
   static ip_address=192.168.10.2/24
   static routers=192.168.10.1
   static domain_name_servers=192.168.10.1 8.8.8.8
   EOF
   ```
3. **Pi 上でインストーラを実行する**:
   ```bash
   sudo bash ~/csi-logger/install.sh
   ```
4. **Tailscale を接続して Pi の Tailscale IP を確認する**（Kernel 5.10 では `iptables-legacy` を使用）:
   ```bash
   sudo update-alternatives --set iptables /usr/sbin/iptables-legacy
   sudo update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy
   sudo systemctl restart tailscaled
   sudo tailscale up
   tailscale ip -4
   ```
   表示された URL を Windows PC のブラウザで開いてログインすると接続されます。スマホにも Tailscale アプリを入れて同じアカウントでログインすれば、スマホのブラウザから `http://<PiのTailscale IP>:8080` を開けます。
5. **Google Drive (`gdrive:`) を `rclone` で連携する**（SSH ポート転送を使うと Pi にブラウザがなくても Windows のブラウザでそのまま認証できます）:
   Windows の PowerShell から以下で接続：
   ```powershell
   ssh -L 53682:127.0.0.1:53682 pi@raspberrypi.local
   ```
   Pi 上で（`sudo` なしで）実行：
   ```bash
   rclone config
   ```
   - `n` (New remote) → Name: `gdrive` → Storage: `drive`（Google Drive）
   - `client_id` / `client_secret` / `root_folder_id` / `service_account_file`: 空のまま Enter
   - `scope`: `1`（Full access） / `Edit advanced config`: `n`
   - `Use auto config?`: **`y`**
   - 表示された `http://127.0.0.1:53682/auth?state=...` を Windows PC のブラウザで開いて Google アカウントで許可します。
   - 完了後、`rclone lsd gdrive:rpi4_nexmon_csi` や Web UI の「🔄 今すぐ区切ってDrive同期」で転送を確認します。
6. **再起動テスト（永続化の確認）**:
   ```bash
   sudo reboot
   ```
   再起動後に再度 SSH で入り、以下を確認します：
   ```bash
   uname -r
   sudo dmesg | grep -i brcmfmac | tail -n 5
   sudo systemctl status csi-logger --no-pager
   tailscale ip -4
   ```

### Google Drive 上のフォルダ構造と CSV 連携

Google Drive (`gdrive:rpi4_nexmon_csi`) には以下の日付別フォルダ構成で自動保存されます：

```text
rpi4_nexmon_csi/
├── labels.csv                # 全期間のイベント・メモ履歴
├── labels.jsonl              # 全期間のイベント履歴 (JSON Lines)
├── segments.csv              # 全期間の .pcap セグメント一覧 (相対パス・時刻・ラベル・メモ付き)
└── YYYY-MM-DD/
    ├── labels_YYYY-MM-DD.csv
    ├── segments_YYYY-MM-DD.csv
    └── pcap/
        └── csi_YYYYMMDD_HHMMSS_<presence>_<window>.pcap
```

`segments.csv` の各行には `segment_id`、`relative_path`（例: `2026-11-24/pcap/csi_20261124_002500_in_room_closed.pcap`）、`start_epoch_ms`、`end_epoch_ms`、`label_class`（`in_room__closed` 等4クラス）、`memos` が入っています。Python (`pandas`) で `segments.csv` と `labels.csv` を紐づけて読み込む例：

```python
import pandas as pd
from pathlib import Path

root = Path("/content/drive/MyDrive/rpi4_nexmon_csi")  # Colab / G:ドライブ等のパス
segments = pd.read_csv(root / "segments.csv")
labels = pd.read_csv(root / "labels.csv")

# 1) segment_id でイベント一覧を紐づける
merged = segments.merge(
    labels.groupby("segment_id")["memo"].apply(lambda s: " | ".join(s.dropna())).reset_index(),
    on="segment_id",
    how="left",
)

# 2) 各 .pcap のフルパスを生成
merged["pcap_path"] = merged["relative_path"].map(lambda p: root / p)
print(merged[["segment_id", "label_class", "duration_sec", "memos", "pcap_path"]].head())
```

## 8. バックアップ

セットアップ完了後・Nexmon CSI インストール後に、USB メモリ全体を生イメージとして保存しておく（Win32DiskImager の Read、「Read Only Allocated Partitions」はオフ）。
イメージには Wi-Fi 情報や SSH ホスト鍵が入るため、公開リポジトリには上げない。
