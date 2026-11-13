# 8-Hour Continuous Optimization, Calibration & Verification Master Log (`cfips` v4.6.1+)

- **Observation Window**: `2026-11-13 07:30+08:00` → `2026-11-13 15:30+08:00` (16 Rounds on GitHub Actions 30-min schedule)
- **Target Protocol & Origin**: `nodejp.snowcat77.com:2053/ws?ed=2048` (`101 Switching Protocols` mandatory for 100% of IPs, 0% Error 1034)
- **Quality SLA Hard Gates**:
  1. **Carrier Physical Speed-of-Light & Zero Detours**:
     - China Telecom (CT): Strict sub-95ms (target sub-70ms) APAC Anycast (`#SG`, `#JP`). Max allowed RTT `<= 120.0ms`.
     - China Unicom (CU): Strict sub-215ms (target sub-195ms) APAC Anycast (`#HK`). Max allowed RTT `<= 219.0ms`.
     - China Mobile (CM): Strict sub-75ms (target sub-50ms) CMI Direct (`#HK`). Max allowed RTT `<= 120.0ms`.
  2. **Packet Loss SLA**: Top 10 packet loss `<= 18%` (target `0.0%` loss on >= 80% of nodes).
  3. **100% Origin WebSocket 101 Pass Rate**: 0% Error 1034 / 403 Forbidden.
  4. **Subnet Diversity**: At least 6–12 distinct `/24` subnets per carrier, maximum 3 IPs per `/24` subnet.
  5. **Enterprise Domain Curation**: Proactively source top global enterprise domains running on Cloudflare Enterprise/Business to enrich candidate pools.

---

## 8-Hour Multi-Round Summary Matrix

| Round | Timestamp (+08:00) | GitHub Actions Run & Commit | CT (Nodes / Subnets / Median RTT / Loss / 101) | CU (Nodes / Subnets / Median RTT / Loss / 101) | CM (Nodes / Subnets / Median RTT / Loss / 101) | Calibration & Verification Action |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Round 1** | 07:32 | Run `43328260845` (`14db849`) | `33 IPs / 11 /24` (med `58.2ms`, **100% 0% loss**, **33/33 101**) | `41 IPs / 14 /24` (med `189.0ms`, **100% 0% loss**, **41/41 101**) | `27 IPs / 9 /24` (med `44.6ms`, max 16.7%, **27/27 101**) | **Baseline 8h Ignition**: Verified commit `14db849` with 101/101 (100%) 101 pass, 100% 0% loss on CT & CU. Initiated screening of 27 global enterprise Cloudflare domains for pool expansion. |
| **Round 2** | 07:55 | Run `43331451292` (`b899408`) | `30 IPs / 10 /24` (med `71.7ms`, **100% 0% loss**, **30/30 101**) | `41 IPs / 14 /24` (med `191.8ms`, **100% 0% loss**, **41/41 101**) | `30 IPs / 10 /24` (med `46.8ms`, max 16.7%, **30/30 101**) | **v4.6.2 Enterprise Integration Verification**: Verified commit `b899408`. Enterprise domains (`coinbase`, `crunchyroll`, `postman`) successfully yielded Singapore CT subnets (`104.18.34/35`) and ZeroTrust CU subnets (`162.159.129`). 101/101 (100%) origin WS 101 passed, 0% US detour on CM. |
| **Round 3** | 08:38 | Run `43337674100` (`d89d7f0`) | `30 IPs / 10 /24` (med **`59.9ms`**, **100% 0% loss**, **30/30 101**) | `26 IPs / 9 /24` (med `198.0ms`, **100% 0% loss**, **26/26 101**) | `36 IPs / 12 /24` (med **`44.1ms`**, max 16.7%, **36/36 101**) | **v4.6.3 Domain Expansion & Collector Speedup**: Verified commit `d89d7f0`. CT median latency dropped to 59.9ms, CM median 44.1ms. 92/92 (100%) WS 101 passed. 89 enterprise sources monitored (86 healthy). |

---

## Detailed Round Logs & Real-Time Tuning Records

### Round 1 — 2026-11-13 07:32+08:00 (v4.6.1 Deployment Verification + Enterprise Domain Screening)
- **GitHub Actions Status**: Workflow `Run 43328260845` (Trigger: Push `6bd4e4e`) completed with `conclusion: success` in ~10 minutes, automatically committing `14db849`.
- **Tri-Net Quality Metrics**:
  - **China Telecom (CT)**: 33 IPs across 11 distinct `/24` subnets. RTT range: `44.4ms ~ 80.7ms` (Median: `58.2ms`). Packet loss: **33/33 (100%) with 0.0% packet loss**.
  - **China Unicom (CU)**: 41 IPs across 14 distinct `/24` subnets. RTT range: `179.3ms ~ 213.2ms` (Median: `189.0ms`). Packet loss: **41/41 (100%) with 0.0% packet loss**.
  - **China Mobile (CM)**: 27 IPs across 9 distinct `/24` subnets. RTT range: `39.5ms ~ 64.2ms` (Median: `44.6ms`). 100% genuine Hong Kong CMI routing, zero US detour.
- **WebSocket 101 Verification**: Tested all 101 IPs sequentially against `nodejp.snowcat77.com:2053/ws?ed=2048` -> **101/101 (100%) passed**.
- **Actions Taken**: Sourced and screened 27 top enterprise domains (GitLab, Canva, Notion, Figma, Coinbase, Kraken, etc.) to integrate into `data/cname_pool.json`.

### Round 2 — 2026-11-13 07:55+08:00 (v4.6.2 Enterprise Domain Automated Pipeline Verification)
- **GitHub Actions Status**: Workflow `Run 43331451292` (Trigger: Push `c504c3b`, v4.6.2) completed with `conclusion: success` in ~9 minutes, automatically committing `b899408`.
- **Tri-Net Quality Metrics**:
  - **China Telecom (CT)**: 30 IPs across 10 distinct `/24` subnets (`104.18.34`, `104.18.35`, `104.18.41`, `104.18.42`, `104.18.43`, `172.64.145`, `172.64.146`, `172.64.151`, `172.64.154`, `172.64.229`). RTT range: `46.4ms ~ 82.3ms` (Median: `71.7ms`). Packet loss: **30/30 (100.0%) with 0.0% packet loss**! 0% US detour (9 JP, 21 SG).
  - **China Unicom (CU)**: 41 IPs across 14 distinct `/24` subnets (all `162.159.*`). RTT range: `181.8ms ~ 207.9ms` (Median: `191.8ms`). Packet loss: **41/41 (100.0%) with 0.0% packet loss**! 0% US detour (41 HK).
  - **China Mobile (CM)**: 30 IPs across 10 distinct `/24` subnets (`172.66.131..143`, `104.21.81..88`). RTT range: `35.5ms ~ 54.1ms` (Median: `46.8ms`). Packet loss: max 16.7% (top 10 average <= 10%). 100% genuine Hong Kong CMI routing (`#HK`), 0% US detour (`172.64.144.*` purged 100%).
- **WebSocket 101 Verification**: Concurrently audited all 101 IPs against `nodejp.snowcat77.com:2053/ws?ed=2048` via `scratch/check_b899408_ws.py` -> **101/101 (100.0%) passed** (`HTTP/1.1 101 Switching Protocols`, 0% Error 1034 / 403).
- **Enterprise Domain Integration Effect**: Enterprise domains (`www.coinbase.com`, `www.crunchyroll.com`, `www.postman.com`) were automatically parsed and successfully yielded high-performance subnets (`104.18.34/35.*` for CT SG and `162.159.129.53` for CU HK).
- **Red-Team Self-Test Audit**: Executed `scripts/self_test_audit.py` -> 100% passed all 6 assertions.

### Round 3 — 2026-11-13 08:38+08:00 (v4.6.3 Domain Expansion & Collector Speedup Verification)
- **GitHub Actions Status**: Workflow `Run 43337674100` (Trigger: Push `942bcf3`, v4.6.3) completed with `conclusion: success` in ~7 minutes, automatically committing `d89d7f0`.
- **Tri-Net Quality Metrics**:
  - **China Telecom (CT)**: 30 IPs across 10 distinct `/24` subnets (`104.18.34`, `104.18.35`, `104.18.41`, `104.18.42`, `104.18.43`, `172.64.145`, `172.64.146`, `172.64.151`, `172.64.154`, `172.64.229`). RTT range: `44.7ms ~ 79.1ms` (Median: **`59.9ms`**, dropped by 11.8ms!). Packet loss: **30/30 (100.0%) with 0.0% packet loss**! 0% US detour (9 JP, 21 SG).
  - **China Unicom (CU)**: 26 IPs across 9 distinct `/24` subnets (all `162.159.*`). RTT range: `181.8ms ~ 217.1ms` (Median: `198.0ms`). Packet loss: **26/26 (100.0%) with 0.0% packet loss**! 0% US detour (26 HK).
  - **China Mobile (CM)**: 36 IPs across 12 distinct `/24` subnets (`104.21.81..83`, `172.66.129..143`). RTT range: `34.2ms ~ 55.7ms` (Median: **`44.1ms`**). Packet loss: 18/36 zero loss, max loss 16.7%. 100% genuine Hong Kong CMI routing (`#HK`), 0% US detour (`172.64.144.*` 0%).
- **WebSocket 101 Verification**: Audited all 92 output IPs against `nodejp.snowcat77.com:2053/ws?ed=2048` via `scratch/check_b899408_ws.py` -> **92/92 (100.0%) passed** (`HTTP/1.1 101 Switching Protocols`, 0% Error 1034 / 403).
- **Collector Optimization Effect**: Eliminating redundant duplicate resolution rounds in `src/collector.py` reduced pipeline duration on GitHub Actions down to ~7 minutes while tracking 89 enterprise domain sources (86 healthy).
- **Red-Team Self-Test Audit**: Executed `scripts/self_test_audit.py` -> 100% passed all 6 assertions.



