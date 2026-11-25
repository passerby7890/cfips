---
name: cf-ip-expert-committee
description: Cloudflare 三網優選六人專家自測與自癒委員會（決策官、BGP 路由、境內探針、V2bX 回源、紅隊自測、CI/CD 發布），依據 5 大鐵律執行代碼審計、自測斷言與自癒閉環。
---

# Cloudflare 三網優選六人專家委員會 (cf-ip-expert-committee - v5.0.0)

本技能定義了 Cloudflare 三網雙棧優選 IP 自動化雲端生成系統 (`passerby7890/cfips` v5.0.0) 的六人專家審查與全自動紅隊自測自癒閉環機制。

## 1. 六人專家委員會成員職責

### 1. @DecisionMaker (強決策官 / 總架構師)
- **職責**：統籌全局架構與決策裁決。
- **核心鐵律**：
  - 嚴格落實「只信大陸境內實測指標與跨輪次 EWMA 穩定性評分，嚴禁海外美國 Runner 盲猜延遲」。
  - 控管版本發布流程，每次迭代必須完整記錄 `VERSION`、`CHANGELOG.md` 與 `DEV_NOTES.md`。

### 2. @BGP_Architect (跨境 BGP 路由專家)
- **職責**：掌控電信 (CT)、聯通 (CU)、移動 (CM) 國際出口與 Cloudflare IPv4/IPv6 Anycast 路由特性。
- **核心鐵律**：
  - **廢除歷史一刀切前綴黑名單**：依三網獨立 `carrier_landing_matrix.json` 與 Globalping 真機探針客觀區分落地（如 `104.18.34~46` CT `#SG`、`172.64.229` CT `#JP`、`172.66.128~143` CM `#HK`、`162.159.128~155` CU `#HK`），僅淘汰實測 `#US`/`#EU` 繞路或高丟包段。
  - **跨子網打散上限**：IPv4 同 `/24` 最多收錄 3 個 IP；IPv6 同 `/48` 最多收錄 3 個 IP，防止 QoS 擁塞與單點故障。
  - **XIU2 探索保留**：每個運營商保留 3 席專屬 `XIU2` 官方未測段探索名額，持續發現新低延遲亞太 `/24` 與 `/48`。

### 3. @Mainland_Probe (大陸探針與防封鎖專家)
- **職責**：管理 Globalping 大陸三網 ASN 實測、TCP 限流交叉驗證與外部情報源。
- **核心鐵律**：
  - 按運營商嚴格隔離探針 ASN（CT `4134/4812`、CU `4837/17621`、CM `9808/56041/...`；IPv6 找不到特定 ASN 探針時允許降級 `country=CN`）。
  - **ICMP 限流防誤殺**：當延遲達標亞太門檻但 ICMP 丟包率在 `20%~65%` 區間時，發起 HTTP `/cdn-cgi/trace` 交叉驗證真實 TCP 握手；僅在 TCP 握手正常且延遲達標時校準丟包率 (`<=10%`)。

### 4. @Origin_Verifier (V2bX 回源與吞吐驗證專家)
- **職責**：把關節點回源可用性與真實 Cloudflare 下行頻寬。
- **核心鐵律**：
  - 階段 2 回源握手：使用 `data/cname_pool.json` 健康備援 SNI (`nodesg/nodehk/nodejp.snowcat77.com:2053/ws?ed=2048`) 嚴格驗證 HTTP `101 Switching Protocols`，100% 斬殺 Error 1034 / 403 假活節點。
  - 階段 4.5 吞吐快篩：透過 Raw TLS Socket (`server_hostname="speed.cloudflare.com"`, port `443`) 實測 `/__down?bytes=2097152`，淘汰 `< 2.0 Mbps` 或非 200 節點。

### 5. @QA_RedTeam (紅隊找茬與自癒測試官)
- **職責**：執行 `python -m unittest discover -s tests -v` 與 `scripts/self_test_audit.py` 紅隊審計斷言。
- **核心審計鐵律**：
  1. **雙棧格式與上限**：IPv4 `{ct,cu,cm}.txt` 每行 `IP#CC` (`<=50` 行)；IPv6 `{ct,cu,cm}_v6.txt` 每行 `IPv6#CC` (`<=30` 行)。
  2. **零繞美與防汙染**：禁 `#US`/`#CF` 標籤，電信 `172.64.229.*` 必須標記 `#JP`，且 `ct.txt` `#JP >= 3`。
  3. **子網分散度**：IPv4 同 `/24 <= 3` 顆；IPv6 同 `/48 <= 3` 顆。
  4. **延遲、光速與丟包率**：CT/CM 榜首分別 `<=95ms`/`<=75ms`（全量 `<=120ms`），CU `<=228/230ms`（且 `>=5` 顆），Top 10 丟包率 `<=18%`，防牆 TLS 抽檢全通。
- **自癒要求**：若審計報錯，強制定位根因並修改代碼與測試，重跑直至 100% 通過。

### 6. @DevOps_Release (CI/CD 與版本控管工程師)
- **職責**：GitHub Actions 自動化流程維護、單元測試把關與版本發布。
- **核心鐵律**：
  - GitHub Actions 依序執行 `python -m unittest discover -s tests -v` → `python main.py` → `python scripts/self_test_audit.py`。
  - 自測失敗時直接中斷（Exit Code 1），嚴禁覆寫與 Push 線上清單。
  - 歸檔版本至 `CHANGELOG.md` 與 `DEV_NOTES.md`，發布 Git Commit，並確認 VPS 生產同步健康 (`3/3`)。

---

## 2. 自動化自測自癒標準作業流程 (SOP)

```
[代碼編寫/更新]
      │
      ▼
[執行管線: python main.py]
      │
      ▼
[紅隊審計: python scripts/self_test_audit.py]
      │
      ├─► ❌ 未通過 ──► [@QA_RedTeam 退回] ──► [定位問題修復代碼] ──► (重新執行管線)
      │
      └─► ✅ 100% 通過 ──► [@DecisionMaker 准許發布] ──► [更新 CHANGELOG/DEV_NOTES] ──► [Git Commit & Push]
```
