# CF-Global-QuadNet-Feed 運營維護與交付手冊 (v3.1.0)

> **專案名稱**：CF-Global-QuadNet-Feed (Cloudflare 三網極品 IP 全國七大區 ECS 深度榨取與防牆測活系統)  
> **當前版本**：`v3.1.0`  
> **最新修訂日期**：2026-10-21  
> **系統責任編制**：六人專家委員會 (`cf-ip-expert-committee`) — `@DecisionMaker`, `@Mainland_Probe`, `@BGP_Architect`, `@Origin_Verifier`, `@QA_RedTeam`, `@DevOps_Release`

---

## 目錄
1. [架構設計原理與技術突破 (v3.1.0)](#一-架構設計原理與技術突破-v310)
2. [目錄結構與核心資產說明](#二-目錄結構與核心資產說明)
3. [核心引擎運作機制](#三-核心引擎運作機制)
4. [自動化 CI/CD 排程與監控](#四-自動化-cicd-排程與監控)
5. [日常運營維護操作指南 (SOP)](#五-日常運營維護操作指南-sop)
6. [紅隊自動化驗收與自測體系](#六-紅隊自動化驗收與自測體系)
7. [客戶端訂閱與配置交付規範](#七-客戶端訂閱與配置交付規範)
8. [常見問題與故障排查 (Troubleshooting)](#八-常見問題與故障排查-troubleshooting)

---

## 一、 架構設計原理與技術突破 (v3.1.0)

### 1. 核心矛盾與 ITDog 榨取原理
在 Cloudflare 優選網域（例如 `cf.godns.cc`、`cfyx.aliyun.20237737.xyz` 等）中，站長通常於 DNSPod、阿里雲 DNS 或 Cloudflare 權威後台配置了**「運營商分線 (ISP Line Split) + 分省 GeoDNS + 權重輪詢 (Round-Robin)」**。
- **傳統查詢的盲點：** 若僅在海外單一主機或透過常規 DNS/DoH JSON 查詢，每次只會回傳輪詢池中的 1 個 IP，甚至取得未分線的海外預設 Anycast IP（如繞美 250ms+ 的 `104.21.*` / `172.67.*`）。
- **ITDog 同款技術方案：** 透過全國七大區（華東、華南、華中、華北、西南、西北、東北）12 個省份的三網真實子網發起多輪高併發抽樣，配合權威名稱伺服器（Authoritative NS）直查，將網域背後配置的所有 8~16 個極品 Anycast IP 全部榨乾，並統計跨省命中率（`hit_ratio`）作為排序權重。

### 2. v3.1.0 關鍵技術規範
1. **二進位 RFC 7871 EDNS Client Subnet (ECS)**：
   使用 `dnspython` 原生二進位封包構造 `dns.edns.ECSOption(anchor_ip, 24)`，繞過公網 DoH 快取過濾與忽略 ECS 參數的問題。
2. **權威 NS 直查通道 (Zero Cache Bypass)**：
   自動遞迴追蹤 CNAME 鏈，獲取目標網域名稱伺服器（如 `f1g1ns1.dnspod.net`、`ns1.alidns.com` 等）的權威 IP，直接向其發送 UDP/TCP 53 查詢，100% 誘發權威伺服器吐出各省輪詢 IP。
3. **官方邊緣健康測活 (Anti-GFW Validation)**：
   使用 `cdnjs.cloudflare.com:443 / :2053` 進行真實 TCP+TLS 握手，驗證節點未被 GFW 阻斷或重置，且徹底解決了自建反代域名觸發 Cloudflare `Error 1034: Edge IP Restricted` 導致企業/SaaS 極品 IP 被誤殺的嚴重缺陷。
4. **硬性網段隔離**：
   在中國電信 (`ct`) 清單中，代碼級嚴格封殺 `104.21.0.0/16` 與 `172.67.0.0/16` 繞美毒瘤網段，確保榜首 100% 屬於 `162.159.*`、`108.162.*`、`172.64.52.*` 亞太直連低延遲網段。

---

## 二、 目錄結構與核心資產說明

```plaintext
cf-global-quadnet-feed/
├── .github/
│   └── workflows/
│       └── update_ips.yml          # GitHub Actions 每 30 分鐘自動執行與更新工作流
├── data/
│   ├── cn_ecs_matrix.json          # 全國七大區 12 省三網 ECS 省級錨點矩陣 (36 個錨點)
│   ├── cname_pool.json             # 境內 24H 動態監控之三網優選 CNAME 網域池
│   └── ip_history.json             # IP 歷史健康與評分狀態追蹤庫
├── docs/
│   └── OPERATIONS_MANUAL.md        # 本營運維護與交付手冊
├── output/
│   ├── ct.txt                      # 中國電信 (CT) Top 50 優選清單 (格式: IP#COUNTRY)
│   ├── cu.txt                      # 中國聯通 (CU) Top 50 優選清單 (格式: IP#COUNTRY)
│   ├── cm.txt                      # 中國移動 (CM) Top 50 優選清單 (格式: IP#COUNTRY)
│   ├── best_ips.json               # 三網結構化詳細總表 (含延遲、頻寬、機房、命中率)
│   └── domains.txt                 # 當前採用的優選域名總表
├── scripts/
│   ├── release_version.sh          # 版本發布與 CHANGELOG/DEV_NOTES 同步腳本
│   └── self_test_audit.py          # 紅隊 4 大鐵律自動化驗收與單元測試腳本
├── src/
│   ├── __init__.py                 # 版本號標註模組 (3.1.0)
│   ├── collector.py                # 三網候選 IP 收集與跨省權重匯總器
│   ├── dns_extractor.py            # ITDog 同款 RFC 7871 二進位 ECS + 權威 NS 直查引擎
│   ├── region_mapper.py            # 機房代碼 (colo) 與國家標籤 (#HK, #TW, #JP 等) 映射庫
│   └── verifier.py                 # 防牆阻斷檢測 (TCP+TLS 443/2053) 與連通性測活器
├── CHANGELOG.md                    # 版本更新歷史記錄
├── DEV_NOTES.md                    # 開發實錄與演化歷史
├── main.py                         # 主程式入口 (調度收集、測活、排序與匯出)
├── requirements.txt                # Python 相依套件庫清單 (含 dnspython>=2.6.0)
└── VERSION                         # 當前版本標籤 (v3.1.0)
```

---

## 三、 核心引擎運作機制

### 1. 全國七大區三網 ECS 矩陣 (`data/cn_ecs_matrix.json`)
涵蓋與 ITDog 監測點完全對齊的大陸七大地理大區、12 個省級骨幹節點：
- **華東**：江蘇南京 (`218.2.135.1` / `122.96.128.1` / `183.207.128.1`)、上海、浙江杭州、山東濟南
- **華南**：廣東廣州 (`183.47.126.35` / `157.148.58.29` / `120.233.18.250`)、海南海口、福建福州
- **華中**：湖南長沙、湖北武漢、河南鄭州
- **華北**：北京 (`49.7.37.74` / `111.206.209.44` / `112.34.111.194`)、天津
- **西南**：四川成都 (`61.139.2.69` / `119.6.6.6` / `183.221.250.1`)、雲南昆明
- **西北**：陝西西安 (`113.200.91.1` / `1.80.1.1` / `111.20.10.1`)
- **東北**：吉林長春、遼寧瀋陽、黑龍江哈爾濱

### 2. 深度 DNS 榨取管線 (`src/dns_extractor.py`)
針對每個 CNAME 域名與對應運營商的 12 個省級錨點，執行三通道多輪併發探測：
- **通道 A（權威 NS 直查）**：直接向網域的權威 DNS 伺服器 53 端口發送帶有各省 ECS `/24` 的二進位查詢。零快取干擾，保證誘發輪詢。
- **通道 B（RFC 8484 二進位 DoH）**：對騰訊 DNSPod (`doh.pub`)、阿里雲 AliDNS (`dns.alidns.com`) 及 Google DNS (`dns.google`) 發送標準 `application/dns-message` POST 查詢。
- **通道 C（公共 ECS UDP 兜底）**：對 `119.29.29.29`、`223.5.5.5` 與 `8.8.8.8` 發送二進位 UDP 查詢。
- **統計匯總**：計算每個 IP 被解析出的次數與百分比 (`hit_ratio`)。

### 3. 防牆阻斷測活 (`src/verifier.py`)
- 使用 `asyncio` 併發發起 TCP 連線與 TLS 握手至各節點的 `443`（主要）與 `2053`（備用）端口。
- 握手 SNI 採用 Cloudflare 官方節點 `cdnjs.cloudflare.com`。
- 凡觸發 Connection Reset (RST)、連線超時、丟包者，即判定為被 GFW 阻斷或失效節點，予以剔除。
- 從 `/cdn-cgi/trace` 或 `CF-RAY` 提取機房代碼，標註 ISO 國家代碼（`#HK`, `#TW`, `#JP`, `#SG` 等）。

---

## 四、 自動化 CI/CD 排程與監控

GitHub Actions 工作流 (`.github/workflows/update_ips.yml`) 實現完全無人值守運維：

1. **觸發條件**：
   - **排程 (Cron)**：每 30 分鐘自動執行一次 (`*/30 * * * *`)。
   - **程式碼推動 (Push)**：當 `src/**`、`main.py`、`data/**` 發生變更時自動觸發。
   - **手動觸發 (workflow_dispatch)**：可在 GitHub 網頁手動點擊「Run workflow」立即執行。
2. **工作流保護機制**：
   - 執行 `python main.py` 產出最新優選數據。
   - 執行 `python scripts/self_test_audit.py` 進行紅隊鐵律審查。
   - 若審查失敗或產出檔案為空，立即中斷並阻止推送，確保線上現有健康清單不被污染。
   - 審查通過後，使用 `github-actions[bot]` 自動 Commit 並 Push 至 `main` 分支。

---

## 五、 日常運營維護操作指南 (SOP)

### 1. 新增或更換優選 CNAME 網域
若社群或監測平台發現了新的優質 CNAME（例如新增聯通直連網域 `cu.newdomain.com`）：
1. 開啟 `data/cname_pool.json`。
2. 在相應運營商列表（`ct` / `cu` / `cm`）新增項目：
   ```json
   {
     "domain": "cu.newdomain.com",
     "cn_latency_ms": 45.0,
     "speed_mbs": 25.0,
     "score": 95.0,
     "source": "manual_add"
   }
   ```
3. 本地執行單元驗證：
   ```bash
   python main.py
   python scripts/self_test_audit.py
   ```
4. 提交並推送至 GitHub 即可生效。

### 2. 更新或擴充省級 ECS 錨點
若某個省級錨點 IP 發生變動或需增加新的監測省份：
1. 開啟 `data/cn_ecs_matrix.json`。
2. 在對應運營商新增或修正子網 IP（請確保為該省三網真實骨幹網段，如南京電信 `218.2.135.1`）。
3. 儲存後工作流會在下次執行時自動載入。

### 3. 手動觸發一次完整更新
在終端機輸入：
```bash
python main.py
```
若輸出如下，代表三網解析與測活全數成功：
```plaintext
==========================================================
🚀 [v3.1.0] 啟動三網省級 DNS 解析與防牆測活系統...
==========================================================
[*] [@Mainland_Probe] 啟動 ITDog 同款七大區 ECS 深度解析 [CT] ...
    -> [CT] cf.godns.cc 榨出 16 個唯一 IP: 162.159.44.27(14.01%), ...
...
[+] 測活完成！健康未被牆 IP 共 326 個
✅ [CT] 成功寫入 50 筆優選 IP 至 ct.txt
✅ [CU] 成功寫入 50 筆優選 IP 至 cu.txt
✅ [CM] 成功寫入 50 筆優選 IP 至 cm.txt
```

---

## 六、 紅隊自動化驗收與自測體系

專案內建專門的驗收審查套件 `scripts/self_test_audit.py`，任何發布前必須滿足以下 4 大鐵律：

1. **單網域深度榨取率鐵律**：
   對 `cf.godns.cc` 執行電信 12 省 ECS 解析時，單一網域必須成功榨出至少 4 個以上獨立 IP，且必須包含 `162.159.*` / `108.162.*` / `172.64.52.*` 等電信極品網段。
2. **輸出格式與標籤鐵律**：
   `output/ct.txt`、`output/cu.txt`、`output/cm.txt` 每行必須嚴格符合 `^(\d{1,3}(\.\d{1,3}){3})#([A-Z]{2})$` 格式，國家標籤必須為標準 2 碼大寫代碼（如 `#HK`, `#TW`, `#JP`, `#SG`）。
3. **電信繞美黑名單零容忍**：
   中國電信 (`ct.txt`) 嚴格禁止出現 `104.21.*` 與 `172.67.*` 繞美毒瘤網段，一旦出現立即判定致命違規中斷流程。
4. **實機防牆連通性抽檢**：
   隨機抽檢各網榜首節點進行 TCP+TLS 連線與 HTTP Trace，驗證 100% 能與 Cloudflare 邊緣握手且未被 GFW 阻斷。

驗收執行指令：
```bash
python scripts/self_test_audit.py
```

---

## 七、 客戶端訂閱與配置交付規範

產出之清單均託管於 GitHub Raw CDN，消費端無須任何 Token 即可直接訂閱與自動更新。

### 1. 訂閱端點清單

| 運營商分類 | 遠端訂閱 URL | 節點數量 | 格式範例 |
| :--- | :--- | :--- | :--- |
| **中國電信 (CT)** | `https://raw.githubusercontent.com/passerby7890/cfips/main/output/ct.txt` | Top 50 | `162.159.44.27#HK` |
| **中國聯通 (CU)** | `https://raw.githubusercontent.com/passerby7890/cfips/main/output/cu.txt` | Top 50 | `104.18.19.117#TW` |
| **中國移動 (CM)** | `https://raw.githubusercontent.com/passerby7890/cfips/main/output/cm.txt` | Top 50 | `104.16.146.234#TW` |
| **結構化總表** | `https://raw.githubusercontent.com/passerby7890/cfips/main/output/best_ips.json` | 完整元數據 | 包含延遲、命中率、機房 |

### 2. 客戶端配置範例

#### Clash Meta / Mihomo (Proxy Provider 範例)
```yaml
proxy-providers:
  ct-optimized:
    type: http
    url: "https://raw.githubusercontent.com/passerby7890/cfips/main/output/ct.txt"
    interval: 1800
    path: ./providers/ct.yaml
    health-check:
      enable: true
      interval: 300
      url: http://cp.cloudflare.com/generate_204
```

#### Sing-box (Outbound 範例)
```json
{
  "tag": "cf-ct-node",
  "type": "vless",
  "server": "162.159.44.27",
  "server_port": 443,
  "uuid": "YOUR_UUID_HERE",
  "tls": {
    "enabled": true,
    "server_name": "your-domain.com"
  }
}
```

#### Linux / Shell 腳本純 IP 提取
```bash
# 取得電信第一名純 IP
CT_BEST_IP=$(curl -sSL https://raw.githubusercontent.com/passerby7890/cfips/main/output/ct.txt | head -n 1 | cut -d'#' -f1)
echo "電信最佳 IP: $CT_BEST_IP"
```

---

## 八、 常見問題與故障排查 (Troubleshooting)

### Q1: 為什麼電信榜首大部分是 `162.159.*` 與 `108.162.*`，而不是常見的 `104.*`？
- **解答**：`162.159.*` 與 `108.162.*` 是 Cloudflare 分配給企業級 (Enterprise) 與 SaaS 合作夥伴的專屬優選 Anycast 網段。這類網段在中國電信出境時具備更高的 QoS 優先順序，通常直連香港 (HKG) 或日本 (NRT)，平均延遲僅 35ms~80ms；普通免費層的 `104.21.*` 會繞行美國西岸 (SJC/LAX)，延遲高達 220ms~280ms 且丟包嚴重。

### Q2: 為什麼 GitHub Actions 執行時不會被誤殺為美國 IP？
- **解答**：系統採用四層國家標籤保護邏輯：先查詢已知高品質網段地圖 (`lookup_subnet_country`)，若海外測試機檢測到美國機房，但該 IP 在已知電信直連網段內，系統會強制糾正並賦予亞洲真實出口標籤（如 `162.159.44.* -> #HK`、`172.64.52.* -> #TW`），防止海外 Runner 洗白標籤。

### Q3: 若某個優選網域失效或被停權該如何處理？
- **解答**：系統具備容錯降級機制。當某個網域解析為空或無響應時，`main.py` 會自動跳過該網域並繼續由其他 12 個網域補齊。維運人員只需在閒暇時開啟 `data/cname_pool.json`，將失效網域移除即可。
