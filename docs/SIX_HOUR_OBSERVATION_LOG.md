# 6-Hour Continuous Optimization & Verification Log (`cfips` v4.6.0+)

- **Window**: `2026-11-12 23:30+08:00` → `2026-11-13 05:30+08:00` (~12 Rounds, 30-min GitHub Actions cadence)
- **Target Backend**: `nodejp.snowcat77.com:2053/ws?ed=2048` (`101 Switching Protocols` required for 100% of output IPs)
- **Strict Quality Criteria**:
  1. **Zero US/EU/Unknown Detours**: Strict mainland China multi-ASN Ping + Speed-of-Light law enforcement (`CT/CM <= 120ms`, `CU <= 225ms`).
  2. **Low Packet Loss**: `<= 18%` worst-case single-probe jitter across 2x3 ping probes (target `0%` on Top 5 per carrier).
  3. **Accurate `#HK`/`#JP`/`#SG`/`#TW` Tagging**: Verified by probe colo/RTT physical law rather than blind wildcard matrix.
  4. **Subnet Diversity & Availability**: Target `>= 10-20` verified clean IPs per carrier when available without lowering quality thresholds.
  5. **GitHub Actions Synced**: Every tuned improvement is committed, pushed to `origin/main`, and verified running cleanly in `.github/workflows/update_ips.yml`.

---

## Round Summary Table

| Round | Time (+08:00) | GH Run ID & Result | CT (Count / Subnets / RTT / 101) | CU (Count / Subnets / RTT / 101) | CM (Count / Subnets / RTT / 101) | Tuning Action Taken |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **R0 (Push Baseline)** | 23:32 | `43248939255` (In Progress) | Local `v4.6.0`: `8 IPs / 3 /24` (`69.6-112.4ms`, `0%`, `8/8 101`) | `10 IPs / 5 /24` (`195.6-212.1ms`, `<=16.7%`, `10/10 101`) | `9 IPs / 3 /24` (`50.8-65.7ms`, `<=16.7%`, `9/9 101`) | Pushed v4.6.0 (`241efea`) to `origin/main`, triggering GH Actions immediately. |
| **R1 (GH Verify + Discovery)** | 23:54 | `43248939255` **success** (`ee69bdc`) | `11 IPs / 4 /24` (`68.5-102.2ms`, `8/11 0%`, `11/11 101`) | `10 IPs / 5 /24` (`204.3-208.8ms`, `9/10 0%`, `10/10 101`) | `9 IPs / 3 /24` (`55.8-60.2ms`, `3/9 0%`, `9/9 101`) | Verified GH Run `43248939255` passed cleanly (`100% 101`, zero US detour). Discovered `172.66.132.1` (CM 38-72ms 0%) & `172.64.151.1` (CT 61-77ms 0%); initiating expansion scan & v4.6.1 runtime optimization. |

---

## Detailed Round Logs

### Round 1 — 2026-11-12 23:54+08:00 (GitHub Actions `43248939255` Verified + Subnet Discovery)
- **GitHub Actions Result**: Run `43248939255` (`241efea`) completed with `conclusion: success` (`15:32:46Z -> 15:53:52Z`, ~21m10s), auto-committing `ee69bdc`.
- **Verified Quality (`observe_round.py`)**:
  - **CT (`11 IPs / 4 /24`)**: `104.18.42.*#SG`, `104.18.35.*#SG`, `104.18.34.*#SG`, `172.64.229.*#JP` (`68.5–102.2ms`, `max_loss=16.7%`, `11/11 WS 101`).
  - **CU (`10 IPs / 5 /24`)**: `162.159.{153,128,152,130,137}.*#HK` (`204.3–208.8ms`, `9/10 0% loss`, `10/10 WS 101`).
  - **CM (`9 IPs / 3 /24`)**: `172.66.{142,141,143}.*#HK` (`55.8–60.2ms`, `9/9 WS 101`) — completely purged `172.64.144.45` and all `~200ms` US detours!
- **Identified Gaps for R1 → R2 Tuning**:
  1. Workflow runtime is ~21 min (risking cron overlap at `*/30 * * * *`) due to sequential Globalping probes (`cn_real_tester.py`) and slow/unresponsive DoH/feed checks.
  2. CM only landed 9 IPs (3 `/24`s) and CT 11 IPs (4 `/24`s); local `2053/ws 101` + Globalping spot-checks already proved `172.66.132.1` (CM `38.0/72.7ms 0%`), `172.64.151.1` (CT `61.6/77.7ms 0%`), and `104.18.46.1` (CT `102.4/105.9ms 0%`) are super-clean!
  3. Notice `172.66.145+` routed to US (`188–208ms`) on CM, so `carrier_landing_matrix.json` should tighten CM's `172.66.128.0/18` to `172.66.128.0/20` (`128..143`) and mark `172.66.144.0/20` as `US`.

### Round 0 — 2026-11-12 23:32+08:00 (v4.6.0 Push & GH Actions Ignition)
- Committed & pushed v4.6.0 (`241efea`) to `origin/main`, superseding old bot output `a9e5b9f` (which still had `172.64.144.45#HK` routing at ~200ms/20%+ loss on CM).
- Verified GitHub Actions run `43248939255` triggered cleanly on `push` event.
- Initiated local port `2053/ws 101` subnet discovery (`0` Globalping quota) to expand verified clean `/24` candidates for CT & CM.

