# 開發筆記與架構演化歷程 (Development Notes)

## [v1.1.0] 演化實錄 (2026-10-20)
### 核心問題
- **問題現象：** GitHub Actions (ubuntu-latest) 位於美東維吉尼亞 Azure 數據中心，連線 Anycast IP 發起 /cdn-cgi/trace 時 Cloudflare 一律就近分配 IAD / SJC 機房。原有的 trace_colo 判定邏輯導致所有 IP 被強制洗成 #US。
- **解決方案：** 
  1. `src/region_mapper.py`：導入 `CF_OFFICIAL_CIDRS` 官方 Anycast 網段辨識器 `is_cf_official_anycast()` 與 4 層優先級 `resolve_country_tag()`。
  2. `src/collector.py`：支援解析種子檔案與情報源行末 `#COUNTRY` 標籤，並萃取 HostMonit / WeTest 探針的境內 `source_colo`。
  3. `src/neighbor_shifter.py`：確保同 /24 鄰居平移 100% 繼承母體 `source_country` 與 `source_colo`。
  4. `src/verifier.py`：隔離海外 Runner 的 `trace_colo`，僅作為通道存活依據，禁止覆蓋原生國家標籤。

