# V2Board (Xiao 版) 節點入口純 IP 獨立替換工具包 (`v2board_syncer`)

本資料夾為**完全獨立的工具包**，已成功部署於您的伺服器 `/www/wwwroot/0n21.com/v2board_syncer`。
純 Python 3 原生標準庫，完全不需要安裝任何額外 pip 套件！

---

## 一、 兩種引擎說明

1. **`v2board_db_sync.py`（【強烈推薦】本地 MySQL 資料庫直接極速替換）**：
   - 直接連線本地 MySQL 修改 `v2_server_vless` 中的 `host` 欄位。
   - 自動讀取面板 `.env` 取得連線帳密，**零 Token、免登入、無過期風險、0 毫秒極速**！
   - 替換後自動執行 `php artisan cache:clear`，客戶端刷新訂閱立即生效！
2. **`v2board_node_sync.py`（遠端 Web API 替換引擎）**：
   - 透過 HTTP REST API 調用 V2Board 後台保存介面，適合不在面板本機時使用。

---

## 二、 檔案結構

```text
/www/wwwroot/0n21.com/v2board_syncer/
├── v2board_db_sync.py     # 【核心】MySQL 資料庫直接極速替換引擎
├── v2board_node_sync.py   # Web API 替換引擎
├── config.json            # 節點設定檔 (節點 ID、三網表、落地國、策略)
├── README.md              # 本手冊
├── cache/                 # 離線容災快取
└── backups/               # 每次替換前的節點備份 JSON
```

---

## 三、 設定檔說明 (`config.json`)

```json
{
  "database": {
    "host": "127.0.0.1",
    "port": 3306,
    "database": "0n21_com",
    "username": "0n21_com",
    "password": "MStrTX7EpG3wtlXM"
  },
  "tables": {
    "ct": "https://raw.githubusercontent.com/passerby7890/cfips/main/output/ct.txt",
    "cu": "https://raw.githubusercontent.com/passerby7890/cfips/main/output/cu.txt",
    "cm": "https://raw.githubusercontent.com/passerby7890/cfips/main/output/cm.txt"
  },
  "nodes": [
    {
      "id": 7,
      "table": "ct",
      "country": "JP",
      "strategy": "random_all",
      "comment": "JP 电信专用-日本(3倍)"
    },
    {
      "id": 6,
      "table": "cm",
      "country": "HK",
      "strategy": "random_all",
      "comment": "HK 移动专用-香港(3倍)"
    }
  ]
}
```

> **選取策略 (`strategy`)**：
> - `"random_all"`：**（推薦）從符合該國別的全部候選池中隨機抽取**，並自動避開目前正在使用的 IP，每次執行保證換入全新優選 IP，極大化防牆阻斷能力！
> - `"random_top3"`：從前 3 名極品中隨機挑選 1 個（小範圍負載分散）
> - `"random_top5"`：從前 5 名極品中隨機挑選 1 個
> - `"top"`：固定選第 1 名最優 IP

---

## 四、 執行指令

### 1. 模擬測試 (不真正寫入資料庫)
```bash
python3 /www/wwwroot/0n21.com/v2board_syncer/v2board_db_sync.py --dry-run
```

### 2. 正式執行替換 (立即將最新純 IP 寫入 MySQL)
```bash
python3 /www/wwwroot/0n21.com/v2board_syncer/v2board_db_sync.py
```

### 3. 命令列直接換指定節點
```bash
# 只換節點 7 為電信日本 IP (全表隨機輪替)
python3 /www/wwwroot/0n21.com/v2board_syncer/v2board_db_sync.py --node 7 --table ct --country JP --strategy random_all

# 只換節點 6 為移動香港 IP (全表隨機輪替)
python3 /www/wwwroot/0n21.com/v2board_syncer/v2board_db_sync.py --node 6 --table cm --country HK --strategy random_all
```

### 4. 寶塔面板（aaPanel）計劃任務設定（推薦）
在寶塔面板後台進入 **「計劃任務」**，新增一個任務：
- **任務類型**：`Shell 腳本`
- **任務名稱**：`V2Board 節點入口優選 IP 輪替`
- **執行週期**：`N分鐘` ➡️ 輸入 `5`（即每 5 分鐘執行一次）
- **腳本內容**：
  ```bash
  /usr/bin/flock -n /tmp/v2board_db_sync.lock /usr/bin/python3 /www/wwwroot/0n21.com/v2board_syncer/v2board_db_sync.py >> /www/wwwroot/0n21.com/v2board_syncer/sync.log 2>&1
  ```
  *(註：也可以簡化填入 `python3 /www/wwwroot/0n21.com/v2board_syncer/v2board_db_sync.py`，寶塔面板會自帶執行日誌檢視。)*

### 5. 原生 Linux Crontab 設定
```bash
# 編輯定時任務
crontab -e

# 加入以下內容 (每 5 分鐘自動更換節點入口 IP)
*/5 * * * * /usr/bin/flock -n /tmp/v2board_db_sync.lock /usr/bin/python3 /www/wwwroot/0n21.com/v2board_syncer/v2board_db_sync.py >> /www/wwwroot/0n21.com/v2board_syncer/sync.log 2>&1
```

### 6. 常駐守護模式 (背景持續循環，免設 Crontab)
```bash
# 每 300 秒 (5 分鐘) 自動循環替換一次
python3 /www/wwwroot/0n21.com/v2board_syncer/v2board_db_sync.py --daemon --interval 300
```


