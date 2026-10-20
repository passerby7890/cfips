# 開發筆記與架構演化歷程 (Development Notes)

## [v1.3.0] 演化實錄 (2026-10-20)
### 核心問題：大規模 IP 池 (2,000+) 帶來之 GitHub Actions 資源與執行時間挑戰
- **問題現象：** 當候選 IP 池自數百擴增至 2,600+ 時，若對每個節點執行 2.5 秒下行吞吐測速，總耗時將超過 80 分鐘 ($2600 \times 2.5\text{s} \approx 6500\text{s}$)，導致每 30 分鐘自動更新的 GitHub Actions 出現堆疊、排隊及超時失敗。
- **解決方案（漏斗型階梯過濾架構）：**
  1. **第一層（100 併發端口快篩）：** 以 0.6s 極短超時非同步高併發掃除未開端口或死 IP，約 10~15 秒完成。
  2. **第二層（50 併發 TLS + WS 101 握手）：** 針對端口存活節點驗證真實 WebSocket 101 Switching Protocols 回源能力。
  3. **第三層（精華限額測速）：** 按初篩握手延遲排序，僅挑選前 80 名精華節點 (`speedtest_candidates_limit: 80`) 送入線程池 (`speedtest_workers: 4`) 執行 2.5 秒吞吐測速，測速時間自 80 分鐘劇降至 30 秒以內。其餘合格節點保留基準分作為備援。
  4. **第四層（評分與多樣性導出）：** 結合 EWMA 歷史平滑、同 C 段分散 (最多 3 個) 與單一國家上限 (`max_ips_per_country: 15`)，保證三網 Top 50 輸出具備香港、台灣、日本、新加坡、韓國等多樣化低延遲組合。

## [v1.1.0] 演化實錄 (2026-10-20)
### 核心問題
- **問題現象：** GitHub Actions (ubuntu-latest) 位於美東維吉尼亞 Azure 數據中心，連線 Anycast IP 發起 /cdn-cgi/trace 時 Cloudflare 一律就近分配 IAD / SJC 機房。原有的 trace_colo 判定邏輯導致所有 IP 被強制洗成 #US。
- **解決方案：** 
  1. `src/region_mapper.py`：導入 `CF_OFFICIAL_CIDRS` 官方 Anycast 網段辨識器 `is_cf_official_anycast()` 與 4 層優先級 `resolve_country_tag()`。
  2. `src/collector.py`：支援解析種子檔案與情報源行末 `#COUNTRY` 標籤，並萃取 HostMonit / WeTest 探針的境內 `source_colo`。
  3. `src/neighbor_shifter.py`：確保同 /24 鄰居平移 100% 繼承母體 `source_country` 與 `source_colo`。
  4. `src/verifier.py`：隔離海外 Runner 的 `trace_colo`，僅作為通道存活依據，禁止覆蓋原生國家標籤。
