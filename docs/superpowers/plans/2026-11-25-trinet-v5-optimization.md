# CF-Global-QuadNet-Feed v5.0.0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement P1 (ICMP-rate-limit TCP verification, CU cap alignment, XIU2 exploration slots), P2 (real `speed.cloudflare.com` throughput sanity check + multi-round EWMA stability scoring), and P3 (full dual-stack IPv6 tri-net pipeline + CI/audit/docs/skill alignment).

**Architecture:** Extend existing modules cleanly (`cn_real_tester`, `region_mapper`, `dns_extractor`, `collector`, `speed_tester`, `scorer`, `main.py`, `self_test_audit.py`) with shared prefix/HTTP helpers and strict TDD coverage under `tests/`. Handle IPv4-only CI runners transparently by verifying IPv6 via Globalping China probes and DoH ECS.

**Tech Stack:** Python 3.12 (`asyncio`, `ipaddress`, `ssl`, `socket`, `requests`, `dnspython`, `unittest`), GitHub Actions (`ubuntu-latest`), Globalping API v1.

**Spec:** `docs/superpowers/specs/2026-11-25-trinet-v5-optimization-design.md`

## Global Constraints
- Zero fabricated latencies/countries; zero Error 1034 in outputs.
- Output IPv4: `output/{ct,cu,cm}.txt`, `IP#CC`, `<= 50` lines, `<= 3` per `/24`, CT `#JP >= 3`.
- Output IPv6: `output/{ct,cu,cm}_v6.txt`, `IPv6#CC`, `<= 30` lines, `<= 3` per `/48`, zero `#US`/`#CF`.
- Backward-compatible `data/ip_history.json` and `output/best_ips.json` schema.
- CI completion under 8 minutes without hitting unrecoverable Globalping rate limits.

## Review Focus
1. IPv6 address formatting & subnet extraction (`get_subnet_prefix("2606:4700:4406::ac40:92ea")` must normalize/extract `/48` consistently, never split on `.` into junk).
2. `data/ip_history.json` legacy format has top-level `{"1.2.3.4": 53.47}` float values; storing EWMA dict under `"__subnet_ewma__"` must never break legacy float readers (`load_history()`).
3. ICMP rate-limit TCP override must ONLY trigger when BOTH `real_ms` is APAC-plausible AND Globalping China TCP actually succeeded (`tcp_ms > 0` and `<= max_good_ms`), never passing dead/GFW-blocked IPs.
4. IPv4-only runner must NOT raise `OSError: [WinError 10051]` / `Network is unreachable` when handling IPv6 candidates.
5. `self_test_audit.py` must validate both IPv4 (`ct/cu/cm.txt`) and IPv6 (`ct_v6/cu_v6/cm_v6.txt`) files without failing on valid IPv6 colons in `<IP>#<CC>` lines.

---

### Task 1: Subnet Prefix & Dual-Stack Anycast Support (`region_mapper.py`, `dns_extractor.py`)

**Files:**
- Modify: `src/region_mapper.py`
- Modify: `src/dns_extractor.py`
- Create: `data/cf_ranges_v6.txt`
- Create: `tests/test_region_and_dns.py`

**Interfaces:**
- Produces: `load_xiu2_cf_v6_cidrs(local_fallback_path=None, timeout=4.0) -> list[str]`
- Produces: `is_cf_official_anycast(ip_str: str) -> bool` (handles both IPv4 & IPv6)
- Produces: `get_subnet_prefix(ip_str: str) -> str` (returns `x.x.x.0/24` for IPv4, normalized `/48` for IPv6)
- Produces: `exhaustive_resolve_domain_for_carrier(domain: str, anchors: list[str], rounds: int = 1, rdtype: str = "A") -> list[dict]`

- [ ] **Step 1: Write failing tests in `tests/test_region_and_dns.py`** testing `get_subnet_prefix` on IPv4/IPv6, `is_cf_official_anycast` on IPv4/IPv6/non-CF IPv6, `load_xiu2_cf_v6_cidrs` fallback loading, and `dns_extractor.exhaustive_resolve_domain_for_carrier(..., rdtype="AAAA")` parsing mocked AAAA answers.
- [ ] **Step 2: Run `python -m unittest tests/test_region_and_dns.py -v` and confirm FAIL.**
- [ ] **Step 3: Populate `data/cf_ranges_v6.txt` from XIU2 `ipv6.txt`, implement dual-stack CIDR checks and `get_subnet_prefix` in `src/region_mapper.py`, and parameterize `rdtype` in `src/dns_extractor.py`.**
- [ ] **Step 4: Run `python -m unittest tests/test_region_and_dns.py -v` and confirm PASS.**
- [ ] **Step 5: Commit Task 1.**

---

### Task 2: P1 ICMP Rate-Limit TCP Cross-Check & Unified HTTP Probe (`cn_real_tester.py`)

**Files:**
- Modify: `src/cn_real_tester.py`
- Create: `tests/test_cn_real_tester.py`

**Interfaces:**
- Consumes: `get_subnet_prefix` from `src.region_mapper`
- Produces: `_probe_http_colo_and_tcp(rep_ip: str, c_key: str, timeout_sec: float = 5.0) -> tuple[str, Optional[float], bool]`
- Produces: `probe_subnet_from_mainland_cn(...)` (supports both IPv4 & IPv6, triggers TCP cross-check when `real_ms <= max_good_ms` and `20.0 <= real_loss <= 65.0`, preserves legacy `get_subnet_24` alias returning `get_subnet_prefix`).

- [ ] **Step 1: Write failing tests in `tests/test_cn_real_tester.py`** verifying:
  - ICMP `36.4ms` with `20%` loss + mocked TCP `45ms` success recovers clean calibrated loss `<= 10.0%` & extracts `colo` without failing the subnet;
  - ICMP `45ms` with `40%` loss + failed/timeout TCP stays rejected (`>= 40%`);
  - IPv6 `AS4134` 422 fallback resolves cleanly to `{country: "CN"}` without crashing.
- [ ] **Step 2: Run `python -m unittest tests/test_cn_real_tester.py -v` and confirm FAIL.**
- [ ] **Step 3: Implement refactored `_probe_http_colo_and_tcp`, IPv6-safe `get_subnet_24`/`get_subnet_prefix` normalization, and 422 ASN fallback for IPv6 probes in `src/cn_real_tester.py`.**
- [ ] **Step 4: Run `python -m unittest tests/test_cn_real_tester.py -v` and confirm PASS.**
- [ ] **Step 5: Commit Task 2.**

---

### Task 3: P2 Real CF Throughput Check & EWMA Subnet Stability (`speed_tester.py`, `scorer.py`)

**Files:**
- Modify: `src/speed_tester.py`
- Modify: `src/scorer.py`
- Create: `tests/test_speed_and_scorer.py`

**Interfaces:**
- Produces: `measure_ip_bandwidth(ip: str, port: int = 443, sni_host: str = "speed.cloudflare.com", duration_limit: float = 1.8, fallback_speed: float = 0.0, bytes_to_fetch: int = 2097152) -> float`
- Produces: `load_subnet_history(history_file) -> dict`, `save_subnet_history(history, history_file)`, `update_subnet_ewma(history, subnet, carrier, country, rtt_ms, loss_rate, ok) -> dict`

- [ ] **Step 1: Write failing tests in `tests/test_speed_and_scorer.py`** covering:
  - Legacy `load_history` ignoring `"__subnet_ewma__"` metadata key cleanly;
  - `update_subnet_ewma` EWMA convergence (`0.7 * prev + 0.3 * cur`), `pass_streak` increment/reset, and Error 1034 (`status != 200` on `speed_tester` returning `0.0` when `fallback_speed=0.0`).
- [ ] **Step 2: Run `python -m unittest tests/test_speed_and_scorer.py -v` and confirm FAIL.**
- [ ] **Step 3: Implement SSL SNI raw socket/urllib3-free clean `measure_ip_bandwidth` in `src/speed_tester.py` and `load_subnet_history`/`save_subnet_history`/`update_subnet_ewma` in `src/scorer.py`.**
- [ ] **Step 4: Run `python -m unittest tests/test_speed_and_scorer.py -v` and confirm PASS.**
- [ ] **Step 5: Commit Task 3.**

---

### Task 4: P3 IPv6 Collection + Main Pipeline Integration (`collector.py`, `main.py`, `discover_xiu2_subnets.py`)

**Files:**
- Modify: `src/collector.py`
- Modify: `main.py`
- Modify: `scripts/discover_xiu2_subnets.py`
- Create: `tests/test_collector_and_pipeline.py`

**Interfaces:**
- Consumes: `load_xiu2_cf_v6_cidrs`, `get_subnet_prefix` (`region_mapper`), `probe_subnet_from_mainland_cn` (`cn_real_tester`), `batch_measure_bandwidth` (`speed_tester`), `update_subnet_ewma` (`scorer`)
- Produces: `collect_carrier_v6_candidates(pool_path, matrix_path) -> dict[str, list[dict]]`
- Produces: `main.py` outputs IPv4 (`ct/cu/cm.txt`, `best_ips.json`) + IPv6 (`ct_v6/cu_v6/cm_v6.txt`, `best_ips.json["ipv6"]`), aligns CU cap `228.0`, reserves exploration/JP slots, integrates EWMA + edge bandwidth verification.

- [ ] **Step 1: Write failing tests in `tests/test_collector_and_pipeline.py`** validating WeTest IPv6 HTML parsing, `_sample_xiu2_extra_candidates` for IPv6 `/48`s, and `main.py` helper selection (`select_diverse_nodes` with `ewma_ms` tie-breaking, CU `228.0` threshold, and `/48` cap `<= 3`).
- [ ] **Step 2: Run `python -m unittest tests/test_collector_and_pipeline.py -v` and confirm FAIL.**
- [ ] **Step 3: Implement IPv6 collector (`collect_carrier_v6_candidates`, `_fetch_wetest_v6_ips`), wire EWMA + `batch_measure_bandwidth` + region-slot budget + dual-stack IPv6 pipeline into `main.py`, and add optional `--ipv6` flag to `scripts/discover_xiu2_subnets.py`.**
- [ ] **Step 4: Run full unit test suite `python -m unittest discover -s tests -v` and confirm ALL PASS.**
- [ ] **Step 5: Commit Task 4.**

---

### Task 5: Audit Upgrade, Targeted XIU2 Hunting, Docs/Skill Sync, E2E Verification & Deploy (`self_test_audit.py`, workflows, docs, GitHub Actions, VPS)

**Files:**
- Modify: `scripts/self_test_audit.py`
- Modify: `.github/workflows/update_ips.yml`
- Modify: `VERSION`, `CHANGELOG.md`, `DEV_NOTES.md`, `ARCHITECTURE_AND_SOP.md`, `README.md`
- Modify: `.agent/skills/cf-ip-expert-committee/SKILL.md` (and `.agents/skills/cf-ip-expert-committee/SKILL.md`)

- [ ] **Step 1:** Update `scripts/self_test_audit.py` to verify `ct_v6.txt`, `cu_v6.txt`, `cm_v6.txt` formatting, `/48` dispersion (`<= 3`), non-`#US`/`#CF` rules, plus `best_ips.json["ipv6"]` presence. Write unit test verifying `self_test_audit.py` rejects bad IPv6 formats and accepts valid `2606:4700::...#HK` lines.
- [ ] **Step 2:** Run targeted XIU2 discovery (`discover_xiu2_subnets.py` / exploration) to check whether any additional clean CT `#JP` or CU `<180ms` subnets are live today; update `data/seeds_{ct,cu}.txt` if verified.
- [ ] **Step 3:** Update `.github/workflows/update_ips.yml` to run `unittest` + `main.py` + `scripts/self_test_audit.py` and push all outputs (`output/*.txt`, `output/*.json`, `data/ip_history.json`, `data/probe_cache.json`).
- [ ] **Step 4:** Sync docs & skill files (`VERSION` -> `v5.0.0`, `CHANGELOG.md`, `DEV_NOTES.md`, `ARCHITECTURE_AND_SOP.md`, `SKILL.md` copies) to reflect actual `v5.0.0` architecture honestly.
- [ ] **Step 5:** Run full verification (`python -m unittest discover -s tests -v; python main.py; python scripts/self_test_audit.py`), inspect real output stats, commit & push to `origin/main`, wait for GitHub Actions green pass, and verify VPS `142.91.98.128` sync (`3/3` healthy!).
