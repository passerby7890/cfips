# V2Board (Xiao 版) 節點入口純 IP 替換引擎（v3.2.0 實戰自癒版）

本工具包專為 **V2Board / Xboard (Xiao 版)** 打造，部署於伺服器 `/www/wwwroot/0n21.com/v2board_syncer`。  
採用純 Python 3 原生標準庫實現，具備 **「原子級直連 MySQL」**、**「Top-K 黏性調度」**、**「1034/GFW 記憶冷卻庫」** 與 **「Globalping 大陸境內真機探測」** 機制，嚴格落實**零中斷（Zero Downtime）**高可用保障！

---

## 🌟 核心特色與五大自癒架構 (v3.2.0)

1. **【Top-K 黏性優選調度 (top_k_sticky)】（解決盲選洗牌問題）**：
   - 每次執行時，先對「目前 MySQL 節點正在使用的現任 IP」進行 `verify_node_origin_handshake()` 與境內探針檢測。
   - 若現任 IP 依然通過 `HTTP 101` 握手、境內暢通且在當期優選清單的前 15 名內，在黏性保持期（如 15 分鐘）內**優先保持現任 IP 不動**！
   - 徹底杜絕過去 `random_all` 每 5 分鐘把 40ms 冠軍 IP 隨機換成次級 IP 的抽盲盒慘況。
2. **【本地持久化黑名單與冷卻記憶庫 (`state_cache.json`)】**：
   - **`error_1034_blacklist` (封禁 24 小時)**：凡觸發 Cloudflare `1034` / `403` 的 IP，24 小時內跳過不測，0ms 快速放行。
   - **`timeout_cooldown` (冷卻 2 小時)**：凡握手超時或被大陸 GFW 阻斷的 IP，冷卻 2 小時，避免同週期內無謂重複測死 IP。
   - **`node_state` (節點狀態追蹤)**：記錄每個 `node_id` 目前綁定的 `current_ip` 與 `assigned_at`。
3. **【大陸境內真機防牆驗證 (`cn_probe.py`)】（防海外 101 假活盲區）**：
   - 透過 Globalping 公開 REST API，指定中國大陸境內實體探針（`locations: [{"country": "CN"}]`）對候選 IP 進行真機 TCP 探測。
   - 若境內探針 100% 丟包阻斷，判定為「國內被牆假活 IP」，立刻打入冷卻池並換血；API 忙碌時支援 Fail-open 保障主流程。
4. **【跨節點防撞車互斥鎖 (`used_ips_in_run`)】**：
   - 同一次同步中，已經分配給 Node 7（電信）的 IP，自動從 Node 6（移動）的候選池排除，防止流量集中單點觸發省牆 QoS。
5. **【0 延遲直連資料庫與零快取干擾】**：
   - 直讀直寫 `v2_server_vless` 的 `host` 欄位。經審計 V2Board 直讀 MySQL，無需清除 Laravel 快取，保障 Session 與登入 100% 穩定！

---

## 📂 檔案結構

```text
/www/wwwroot/0n21.com/v2board_syncer/
├── v2board_db_sync.py     # 【核心】MySQL 資料庫直連替換引擎 (含 Top-K 黏性自癒)
├── state_manager.py       # 【狀態】1034/GFW 黑名單與節點黏性狀態管理器
├── cn_probe.py            # 【探針】Globalping 大陸境內真機 TCP 防牆驗證模組
├── state_cache.json       # 【快取】本地持久化記憶庫 (自動產生與輪替)
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
      "node_id": 7,
      "id": 7,
      "name": "JP 电信专用-日本(3倍)",
      "carrier": "ct",
      "table": "ct",
      "prefer_country": "JP",
      "country": "JP",
      "strategy": "top_k_sticky",
      "top_k": 8,
      "sticky_minutes": 15,
      "enable_cn_probe": true
    },
    {
      "node_id": 6,
      "id": 6,
      "name": "HK 移动专用-香港(3倍)",
      "carrier": "cm",
      "table": "cm",
      "prefer_country": "HK",
      "country": "HK",
      "strategy": "top_k_sticky",
      "top_k": 8,
      "sticky_minutes": 15,
      "enable_cn_probe": true
    }
  ]
}
```

### 選取策略 (`strategy`)
- `"top_k_sticky"`：**（預設強烈推薦）前段班黏性調度**。現任 IP 若健康且在前 15 名內，保持不動；到期或失效時僅從前 8 名 (Top 8) 挑選新血。
- `"top_k_random"`：每次從通過 101 與國內探針驗證的前 8 名 (Top 8) 中隨機輪替。
- `"random_all"`：相容舊版全池隨機。
- `"top"`：優先選取第 1 名，未通過時依序遞補。

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
