# V2Board (Xiao 版) 節點入口純 IP 替換引擎（v4.3.0 五道防護機制版）

本工具包專為 **V2Board / Xboard (Xiao 版)** 打造，部署於伺服器 `/www/wwwroot/0n21.com/v2board_syncer`。  
採用純 Python 3 原生標準庫實現，落實**決策驗收官 (@DecisionMaker)** 制定的「最後選取五道防護閘門」，徹底杜絕 Anycast 路由動態漂移繞美、GFW 阻斷、1034 限制及節點衝突，保障**零中斷（Zero Downtime）**與**真亞太低延遲**！

---

## 🛡️ 最後選取五道防護閘門 (v4.3.0)

```text
[候選 IP 進入選拔]
   │
   ├─► 閘門 1：【記憶庫與三網繞美底線攔截】
   │     ├── 0ms 剔除 state_cache.json 內的 24h (1034) 與 2h (超時/被牆/高延遲漂移) 黑名單
   │     └── 電信(ct)/聯通(cu) 強制攔截 104.18./104.20./104.21./104.26./172.67. 等必繞美西前綴
   │
   ├─► 閘門 2：【跨節點同 IP 與同 /24 網段防撞車】
   │     └── Node 8 (SG 電信)、Node 7 (JP 電信) 與 Node 6 (HK 移動) 禁止共用同 IP，優先打散不同 /24 網段
   │
   ├─► 閘門 3：【事前真實 V2bX 端到端 101 握手】
   │     └── 帶著該節點在 MySQL 中的真實 SNI / Port / WS Path 握手，非 HTTP 101 立即淘汰並冷卻
   │
   ├─► 閘門 4：【大陸境內三網 ASN 探針「雙指標」驗證 (防牆 + 防 Anycast 漂移繞美)】
   │     ├── 依節點 carrier 指定大陸對應運營商 ASN (電信 AS4134 / 聯通 AS4837 / 移動 AS9808)
   │     ├── 指標 A (防 GFW 封鎖)：丟包率必須 <= 50% (不可 100% 丟包)
   │     └── 指標 B (防漂移繞美)：大陸境內實測平均延遲 (avg_rtt) 必須 <= max_cn_rtt_ms (預設 115ms)！
   │         (一旦發現 avg_rtt > 115ms，代表該 Anycast IP 已漂移至美西，當場淘汰並冷卻 2h！)
   │
   └─► 閘門 5：【智慧條件式黏性守護 (Smart Sticky Guard)】
         ├── 若現任 IP 為首選國別 (如 JP)，且 101 + 大陸 RTT <= 115ms 均合格 ➡️ 鎖定保持
         └── 若現任 IP 為備援國別 (如 HK/SG)，僅當候選池中「確實還有未進黑名單的首選國別 (JP) IP」時才解除黏性嘗試升級；若首選國別已全滅，則穩定保持現任備援 IP，杜絕每 5 分鐘反覆斷線！
```

---

## 📂 檔案結構

```text
/www/wwwroot/0n21.com/v2board_syncer/
├── v2board_db_sync.py     # 【核心】MySQL 資料庫直連替換引擎 (含五道防護與智慧黏性)
├── state_manager.py       # 【狀態】1034/GFW/漂移繞美黑名單與節點黏性管理器
├── cn_probe.py            # 【探針】Globalping 大陸三網 ASN 對齊與延遲防漂移模組
├── state_cache.json       # 【快取】本地持久化記憶庫 (自動產生與輪替)
├── config.json            # 節點設定檔 (節點 ID、三網表、落地國、策略、延遲上限)
├── README.md              # 系統技術手冊與操作筆記
├── cache/                 # 離線容災快取目錄
└── backups/               # 每次變更前的節點備份 JSON
```

---

## ⚙️ 設定檔說明 (`config.json`)

```json
{
  "_comment": "V2Board (Xiao 版) 節點入口純 IP 替換設定檔 (v4.3.0 實戰自癒與防繞美漂移版)",
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
      "node_id": 8,
      "id": 8,
      "name": "SG 电信专用-新加坡(3倍)",
      "carrier": "ct",
      "table": "ct",
      "prefer_country": "SG",
      "prefer_countries": ["SG", "JP", "HK"],
      "reject_countries": ["US"],
      "strategy": "top_k_sticky",
      "top_k": 8,
      "sticky_minutes": 15,
      "enable_cn_probe": true,
      "max_cn_rtt_ms": 115
    },
    {
      "node_id": 7,
      "id": 7,
      "name": "JP 电信专用-日本(3倍)",
      "carrier": "ct",
      "table": "ct",
      "prefer_country": "JP",
      "prefer_countries": ["JP", "HK", "SG"],
      "reject_countries": ["US"],
      "strategy": "top_k_sticky",
      "top_k": 8,
      "sticky_minutes": 15,
      "enable_cn_probe": true,
      "max_cn_rtt_ms": 115
    },
    {
      "node_id": 6,
      "id": 6,
      "name": "HK 移动专用-香港(3倍)",
      "carrier": "cm",
      "table": "cm",
      "prefer_country": "HK",
      "prefer_countries": ["HK", "SG", "JP"],
      "reject_countries": ["US"],
      "strategy": "top_k_sticky",
      "top_k": 8,
      "sticky_minutes": 15,
      "enable_cn_probe": true,
      "max_cn_rtt_ms": 115
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
