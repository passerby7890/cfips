# V2Board (Xiao 版) 節點入口純 IP 替換引擎（事前回源測活認證版）

本工具包專為 **V2Board / Xboard (Xiao 版)** 打造，部署於伺服器 `/www/wwwroot/0n21.com/v2board_syncer`。  
採用純 Python 3 原生標準庫實現，具備 **「原子級直連 MySQL」** 與 **「事前端到端真實回源測活（Pre-Flight Origin Verification）」** 機制，嚴格落實**零中斷（Zero Downtime）**高可用保障！

---

## 🌟 核心特色與防護架構

1. **【事前端到端真實回源測活】（核心安全性）**：
   - 在決定將任何 IP 寫入資料庫前，依節點的真實配置（連接埠、SNI、Host 標頭、WebSocket 路徑）建立真實 TCP + TLS 握手與 HTTP/1.1 WebSocket 升級請求。
   - **只有拿到 `101 Switching Protocols`（或原點伺服器正常握手）才視為合格 IP！**
2. **【防 Cloudflare Error 1034 邊緣阻斷機制】**：
   - 解決 Cloudflare Pages / Workers 專用段（如 `162.159.*`, `108.162.*`）被官方啟用 Edge IP Validation (EIV) 導致自訂 SNI 回源出現 1034 阻斷的問題。
   - 測活模組若檢測到 `1034` 或 `403`，立即將該 IP 淘汰 (`[✖ 測活淘汰]`)。
3. **【智能跨區健康備援池】**：
   - 若某節點偏好國別（如 `JP`）候選 IP 全數受限或失效，系統自動在同營運商表中啟用其他通過驗證的健康備援 IP（如 `SG`/`HK` 等 Anycast 節點）。
4. **【嚴格零中斷保護（Zero-Downtime Guarantee）】**：
   - **絕不盲選、絕不寫入未經認證的 IP！** 若表內候選 IP 全數未能通過回源測活，系統立即觸發保護機制，**安全保留當前正常運行的 host，不對 MySQL 做任何破壞性變更**。
5. **【0 延遲直連資料庫】**：
   - 免 Token、免登入、無過期風險，只修改 `v2_server_vless` 的 `host` 欄位，保留 port、tls、network、flow 等所有參數。
6. **【自動刷新面板快取】**：
   - 每次成功變更後，自動調用 `php artisan cache:clear`，客戶端訂閱與面板前台即時生效。

---

## 📂 檔案結構

```text
/www/wwwroot/0n21.com/v2board_syncer/
├── v2board_db_sync.py     # 【核心】MySQL 資料庫直連替換引擎 (含回源測活)
├── config.json            # 節點設定檔 (節點 ID、三網表、落地國、策略)
├── README.md              # 系統技術手冊與操作筆記
├── cache/                 # 離線容災快取目錄
└── backups/               # 每次變更前的節點備份 JSON
```

---

## ⚙️ 設定檔說明 (`config.json`)

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
      "protocol": "vless",
      "table": "ct",
      "country": "JP",
      "strategy": "random_all",
      "comment": "JP 电信专用-日本(3倍)"
    },
    {
      "id": 6,
      "protocol": "vless",
      "table": "cm",
      "country": "HK",
      "strategy": "random_all",
      "comment": "HK 移动专用-香港(3倍)"
    }
  ]
}
```

### 選取策略 (`strategy`)
- `"random_all"`：**（強烈推薦）全池隨機抽取**。自動將目前使用中的 IP 排至最後，每次執行都能輪替到不同且通過測活的優質 IP，大幅提升防阻斷與負載均衡效果。
- `"random_top3"`：從前 3 名候選中隨機挑選 1 個並測活。
- `"random_top5"`：從前 5 名候選中隨機挑選 1 個並測活。
- `"top"`：優先選取第 1 名，若未通過測活則依序遞補。

---

## 🔬 回源測活原理與技術筆記

### 1. 為什麼填網域能通、填某些純 IP 會不通？
- **網域模式**：例如 `www.oopt.eu.cc` 解析至 `104.18.*`、`104.21.*` 等 Cloudflare Standard Anycast IP 段，這些 IP 支援任意 Cloudflare 託管域名的 SNI 代理轉發。
- **純 IP 模式**：部分優選工具挑選到了 `162.159.*` 或 `108.162.*` 段，此類屬於 Cloudflare Pages/Workers/Spectrum 專用網段。Cloudflare 在邊緣啟用了 Edge IP Validation (EIV)，當自訂 SNI（如 `nodejp.snowcat77.com`）直接連向這些 IP 時，邊緣伺服器直接回傳 `HTTP/2 403 Forbidden (Error code: 1034 Edge IP Restricted)`，導致連線中斷。
- **解決方案**：
  1. 上游優選管線加入 `1034` 過濾機制。
  2. 節點替換引擎引入**事前回源測活認證**，所有候選 IP 必須先經由真實節點參數握手成功，才獲准套入資料庫。

### 2. 測活程序握手細節
- **TCP 連線**：直接連接 `cand_ip:node_port`（超時時間 2.5 秒）。
- **TLS 封裝**：使用節點特定的 `server_name` (SNI) 進行 TLS 握手。
- **WS 請求**：發送標準 WebSocket 升級標頭：
  ```http
  GET /ws?ed=2048 HTTP/1.1
  Host: nodejp.snowcat77.com
  Upgrade: websocket
  Connection: Upgrade
  Sec-WebSocket-Key: ...
  Sec-WebSocket-Version: 13
  ```
- **判定依據**：
  - `HTTP/1.1 101 Switching Protocols` ➡️ **測活通過 (100% 成功)**
  - `Cloudflare Error 1034 / 403 / 502 / 521` ➡️ **測活淘汰**

---

## 🚀 執行指令

### 1. 模擬測試（Dry-Run，僅測活不改資料庫）
```bash
python3 /www/wwwroot/0n21.com/v2board_syncer/v2board_db_sync.py --dry-run
```

### 2. 正式執行（測活確認無誤後寫入 MySQL）
```bash
python3 /www/wwwroot/0n21.com/v2board_syncer/v2board_db_sync.py
```

### 3. 單一節點手動測試
```bash
# 測試並更新節點 7 (電信，偏好日本)
python3 /www/wwwroot/0n21.com/v2board_syncer/v2board_db_sync.py --node 7 --table ct --country JP --strategy random_all

# 測試並更新節點 6 (移動，偏好香港)
python3 /www/wwwroot/0n21.com/v2board_syncer/v2board_db_sync.py --node 6 --table cm --country HK --strategy random_all
```

---

## ⏰ 寶塔面板計劃任務設定

進入寶塔面板（aaPanel）後台 ➡️ **「計劃任務」** ➡️ **「新增任務」**：
- **任務名稱**：`V2Board 節點入口優選 IP 測活輪替`
- **任務類型**：`Shell 腳本`
- **執行週期**：`N分鐘` ➡️ `5` 分鐘
- **腳本內容**：
  ```bash
  /usr/bin/flock -n /tmp/v2board_db_sync.lock /usr/bin/python3 /www/wwwroot/0n21.com/v2board_syncer/v2board_db_sync.py >> /www/wwwroot/0n21.com/v2board_syncer/sync.log 2>&1
  ```
- **日誌查看**：直接在寶塔計劃任務清單點擊「日誌」，即可查看每次執行的 IP 測活過程、淘汰原因與最終套入的健康 IP。
