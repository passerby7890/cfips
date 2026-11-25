# CF-Global-QuadNet-Feed v5.0.0 三網優選全棧升級設計規格 (P1 + P2 + P3)

**日期**：2026-11-25  
**目標版本**：`v5.0.0`  
**狀態**：實作中 (`/goal`)

---

## 1. 意圖與問題背景 (Intent & Problem Statement)

根據 2026-11-21 至 2026-11-25 的生產環境實測（GitHub Actions Run `#198`–`#211` 與 VPS `142.91.98.128` 594 次 V2Board 同步紀錄），現行 `v4.6.5` 系統閉環已穩定，但在產出品質與覆蓋完整度上仍有五項具體缺口：

1. **P1（數量震盪與覆蓋單薄）**：
   - `cu.txt` 在 Run `#211` 因 `packets=5` 掉 1 包 (`20.0% > 18.0%`) 及 `219ms` 門檻過嚴（審計基準為 `230ms`）而自 41 顆跌至 15 顆。
   - `cm.txt` 多個直連香港黃金段（`172.66.134.*`, `104.21.85.*`, `172.66.140.*`）因移動省城域網 ICMP 限流出現 `20%~60%` 假丟包遭誤殺。
   - 電信 `ct.txt` 的 `#JP` 僅仰賴單一 `/24` (`172.64.229.0/24`)，缺乏主動淘金預留配額。
2. **P2（單次快照與缺吞吐驗收）**：
   - 現行排名僅看單次 Ping，缺少多輪 **EWMA 歷史穩定度平滑**（既有 `src/scorer.py` 與 `data/ip_history.json` 未接入主流程）。
   - `src/speed_tester.py` 指向失效端點 `node1.snowcat77.com:2053/__down`，無法測出真實 Cloudflare Edge 吞吐能力（改以 SNI `speed.cloudflare.com` 實測 `443/__down` 排除限速/故障節點）。
3. **P3（僅 IPv4、文件滯後）**：
   - 缺乏對 `XIU2/CloudflareSpeedTest/master/ipv6.txt`、WeTest IPv6 及現有域名池 `AAAA` 紀錄的解析與產出（`ct_v6.txt`, `cu_v6.txt`, `cm_v6.txt`），且須相容無出站 IPv6 的 GitHub Actions `ubuntu-latest` Runner。
   - `SKILL.md` 與 `ARCHITECTURE_AND_SOP.md` 仍殘留舊版 v3/v4 靜態封殺描述。

---

## 2. 系統約束與非目標 (Constraints & Non-Goals)

- **真實性底線**：嚴禁在 Runner 或 API 限流時偽造延遲或國家標籤；中國大陸真實路由與 RTT 必須來自 Globalping 境內運營商 ASN 實測。
- **兼容性**：保留 `output/{ct,cu,cm}.txt` (`IP#CC`, `<=50` 行, 同 `/24 <= 3` 顆)、`output/best_ips.json`、`output/cname_health.json`、`output/domains.txt` 既有格式，確保下游 `v2board_syncer` 無痛運作。
- **無 IPv6 Runner 相容性**：GitHub Actions `ubuntu-latest` 無出站 IPv6，因此 IPv6 連通性與 RTT 驗證必須透過 IPv4-reachable 的 Globalping China HTTPS/HTTP + Ping 完成，不得因本機/Runner 無 IPv6 路由而拋錯中斷。
- **CI 時限**：完整管線在 GitHub Actions 須穩定於 8 分鐘內完成。
- **非目標**：不主動修改 V2Board Node 6/7/8 改寫 IPv6 至僅支援單 IP 的 `host` 欄位（VPS 上仍維持 IPv4 主用，另提供獨立 `*_v6.txt` 訂閱與工具支持）。

---

## 3. 核心技術決策 (Approaches & Architectural Design)

### P1：ICMP 限流交叉複驗、門檻對齊與主動淘金配額
- 在 [src/cn_real_tester.py](file:///D:/Nextcloud/NICK/V2board重構/Cloudflare%20best%20ips/cf-global-quadnet-feed/src/cn_real_tester.py) 抽出共用的 `_probe_http_colo_and_tcp(rep_ip, c_key, protocol="HTTP", port=80)` 輔助函式，回傳 `(colo, tcp_ms, ok)`：
  - 當 ICMP ping 取得亞太區間延遲（`CT/CM <= 125ms` 或 `CU <= 230ms`）但 ICMP 丟包落在 `20.0% ~ 65.0%`（單包抖動或省網 ICMP 限流）時，自動觸發 1 次 Globalping HTTP/TCP 複驗。
  - 若大陸 ASN TCP 握手成功（`tcp_ms > 0` 且 `tcp_ms <= max_good_ms` 且收到合法 HTTP 狀態），判定為 ICMP 限流誤報，將有效丟包率修正為 `5.0%`（ICMP `<=25%`）或 `10.0%`（ICMP `<=65%`），保留節點並順便完成真實 `colo` 定標；若 TCP 亦逾時或遠高於門檻則誠實淘汰。
- [main.py](file:///D:/Nextcloud/NICK/V2board重構/Cloudflare%20best%20ips/cf-global-quadnet-feed/main.py) 將 CU `max_allowed_ms` 自 `219.0` 對齊至 `228.0`（與審計標準 `230.0ms` 一致），並為每個運營商保留固定 **XIU2 未測新子網探索名額**（從 24 個配額中確保 `>= 3` 個新段探索與 `#JP` 候選配額）。

### P2：真實 CF Edge 吞吐過濾與 EWMA 跨輪穩定度評分
- 修復 [src/speed_tester.py](file:///D:/Nextcloud/NICK/V2board重構/Cloudflare%20best%20ips/cf-global-quadnet-feed/src/speed_tester.py)：透過標準 TLS SNI `speed.cloudflare.com` 直連目標 `ip:443` 下載 `/__down?bytes=2097152`（限時 `1.8s`），排除非正常轉發、Error 1034 或極端限速（`< 2.0 Mbps`）節點，記為 `edge_speed_mbps`。
- 擴展 [src/scorer.py](file:///D:/Nextcloud/NICK/V2board重構/Cloudflare%20best%20ips/cf-global-quadnet-feed/src/scorer.py) 與 `data/ip_history.json`：保留原有 IP -> 速度鍵相容性，同時在 `data/ip_history.json["__subnet_ewma__"]` 維護每個 `(subnet, carrier)` 的歷史 EWMA 延遲 (`ewma_ms`)、EWMA 丟包率 (`ewma_loss`) 與連續健康次數 (`pass_streak`)。排序鍵結合「有效丟包分檔 + `0.7 * 當次實測延遲 + 0.3 * EWMA 歷史延遲` - 穩定度微調 + 吞吐驗證保障」，減少單輪抖動導致名單劇烈洗牌。

### P3：雙棧 IPv6 三網優選管線與規範文件對齊
- **IPv6 官方網段與解析**：
  - [src/region_mapper.py](file:///D:/Nextcloud/NICK/V2board重構/Cloudflare%20best%20ips/cf-global-quadnet-feed/src/region_mapper.py) 新增 `XIU2_IPV6_URL`、`load_xiu2_cf_v6_cidrs()`、本地後備 `data/cf_ranges_v6.txt`，並統一 `is_cf_official_anycast()` 支援 IPv4 + IPv6。
  - [src/dns_extractor.py](file:///D:/Nextcloud/NICK/V2board重構/Cloudflare%20best%20ips/cf-global-quadnet-feed/src/dns_extractor.py) 以參數 `rdtype="A"|"AAAA"` 支援 ECS `AAAA` 萃取。
- **IPv6 收集、大陸探測與驗證**：
  - [src/collector.py](file:///D:/Nextcloud/NICK/V2board重構/Cloudflare%20best%20ips/cf-global-quadnet-feed/src/collector.py) 新增 `collect_carrier_v6_candidates()` 整合「健康域名 ECS `AAAA` + WeTest IPv6 榜 + `seeds_*_v6.txt` + XIU2 `ipv6.txt` `/48` 抽樣」。
  - [src/cn_real_tester.py](file:///D:/Nextcloud/NICK/V2board重構/Cloudflare%20best%20ips/cf-global-quadnet-feed/src/cn_real_tester.py) 泛化 `get_subnet_prefix(ip)`（IPv4 `/24`、IPv6 `/48`），透過 Globalping 大陸探針實測 IPv6 RTT 與丟包（若運營商專網 ASN 暫無 IPv6 探針而返回 422，回退 `{country: "CN"}` 並誠實標記）；若 Runner 無出站 IPv6，改由 Globalping China HTTPS/HTTP 探針驗證端點可達且無 1034，安全輸出 `output/ct_v6.txt`、`output/cu_v6.txt`、`output/cm_v6.txt`（每檔最多 30 筆、同 `/48 <= 3` 顆）並收錄進 `output/best_ips.json["ipv6"]`。
- **審計與文件同步**：
  - 更新 [scripts/self_test_audit.py](file:///D:/Nextcloud/NICK/V2board重構/Cloudflare%20best%20ips/cf-global-quadnet-feed/scripts/self_test_audit.py) 納入 `*_v6.txt` 格式、`/48` 分散度與禁 `#US`/`#CF` 驗證。
  - 更新 `.github/workflows/update_ips.yml`、`VERSION` (`v5.0.0`)、`CHANGELOG.md`、`DEV_NOTES.md`、`ARCHITECTURE_AND_SOP.md` 及 `.agent/skills/cf-ip-expert-committee/SKILL.md`（同步兩處 skill 複本）。
