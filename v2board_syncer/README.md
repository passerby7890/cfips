# V2Board (Xiao 版) 節點入口純 IP 獨立替換工具包 (`v2board_syncer`)

本資料夾為**完全獨立的工具包**，您可以整包複製到任何 V2Board 伺服器直接執行，**完全不依賴母專案的其他檔案，也不需安裝任何額外 pip 套件 (純 Python 3 原生標準庫)**。

---

## 目錄檔案結構

```text
v2board_syncer/
├── v2board_node_sync.py   # 核心替換腳本 (支援 CLI 單換、批次換、Crontab 與 Daemon 常駐)
├── config.json            # 獨立設定檔 (面板網址、Token、三張表來源、節點清單)
├── README.md              # 本說明文件
├── cache/                 # 自動快取目錄 (三網表斷網容災備份)
└── backups/               # 自動備份目錄 (每次換 IP 前的舊節點 JSON 備份)
```

---

## 快速設定 (`config.json`)

只需修改 `v2board_syncer/config.json` 中的 `url` 與 `token`：
```json
{
  "v2board": {
    "url": "http://127.0.0.1",
    "token": "貼上您的管理員_Token"
  },
  "tables": {
    "ct": "https://raw.githubusercontent.com/passerby7890/cfips/main/output/ct.txt",
    "cu": "https://raw.githubusercontent.com/passerby7890/cfips/main/output/cu.txt",
    "cm": "https://raw.githubusercontent.com/passerby7890/cfips/main/output/cm.txt"
  },
  "nodes": [
    {
      "id": 7,
      "protocol": "vless",
      "table": "ct",
      "country": "JP",
      "strategy": "top",
      "comment": "JP 电信专用-日本(3倍)"
    },
    {
      "id": 6,
      "protocol": "vless",
      "table": "cm",
      "country": "HK",
      "strategy": "top",
      "comment": "HK 移动专用-香港(3倍)"
    }
  ]
}
```

> **選取策略 (`strategy`)**：
> - `"top"`：固定選第 1 名最優 IP（預設）
> - `"random_top3"`：從前 3 名極品中隨機挑選 1 個（分散負載）
> - `"random_top5"`：從前 5 名極品中隨機挑選 1 個

---

## 執行指令

### 1. 模擬測試 (不真正寫入面板)
```bash
python3 v2board_node_sync.py --dry-run
```

### 2. 單次手動執行 (按 config.json 立即替換)
```bash
python3 v2board_node_sync.py
```

### 3. 命令列直接換指定 node
```bash
# 只換節點 7 為電信日本最優 IP
python3 v2board_node_sync.py --node 7 --table ct --country JP

# 只換節點 6 為移動香港最優 IP (從前三隨機選)
python3 v2board_node_sync.py --node 6 --table cm --country HK --strategy random_top3
```

### 4. 內建守護模式 (背景常駐定時循環，免設 Crontab)
```bash
# 每 1800 秒 (30 分鐘) 自動循環替換一次
python3 v2board_node_sync.py --daemon --interval 1800
```

### 5. Linux Crontab 定時排程
在伺服器輸入 `crontab -e`，加入：
```bash
# 每 30 分鐘執行一次
*/30 * * * * /usr/bin/python3 /opt/v2board_syncer/v2board_node_sync.py >> /var/log/v2board_sync.log 2>&1
```
