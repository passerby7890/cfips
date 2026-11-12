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

