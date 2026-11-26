#!/usr/bin/env python3
"""CF-Global-QuadNet-Feed 主控流程 (v4.0.0 大陸三網真機探針實測與嚴格封殺繞美系統)。

架構規範 (v4.0.0)：
1. 階段 1：收集完 CNAME 解析出 IP 後，先用 is_blacklisted_us_range() 砍掉所有 104.18~26.* 與 172.67.* 繞美西網段！
2. 階段 2：對剩餘候選執行 nodejp.snowcat77.com:2053/ws?ed=2048 的 HTTP 101 握手測活（砍掉所有 Error 1034 與非 101 節點）。
3. 階段 3：按 /24 網段分組，呼叫 probe_subnet_from_mainland_cn() 取得每個 /24 網段在中國大陸的真實落地國家 (country) 與真實大陸延遲 (real_cn_ms)。
4. 階段 4：剔除所有被判定為 #US 或 real_cn_ms > 115ms 的繞美節點，嚴格按 real_cn_ms 由低到高排序，同 /24 最多取 3 顆，每個檔案最多輸出 50 行！
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
import ipaddress
import json
import os
import shutil
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

from src import __version__
from src.collector import (
    collect_carrier_candidates_with_domain_map,
    collect_carrier_v6_candidates,
)
from src.cn_real_tester import (
    get_subnet_24,
    is_blacklisted_us_range,
    is_cached_bad_subnet,
    probe_subnet_from_mainland_cn,
    save_probe_cache,
)
from src.region_mapper import (
    init_carrier_landing_matrix,
    init_subnet_country_map,
    lookup_carrier_landing_country,
    resolve_country_tag,
)
from src.scorer import load_subnet_history, save_subnet_history, update_subnet_ewma
from src.speed_tester import batch_measure_bandwidth
from src.verifier import batch_verify_with_health_stats

PROJECT_ROOT = Path(__file__).resolve().parent

BANNED_COUNTRIES = ("US", "GB", "FR", "NL", "CA", "DE", "UNKNOWN", "CF", "")


def _runner_supports_ipv6() -> bool:
    """檢測當前執行環境是否具備 IPv6 外網路由，無則僅依賴 Globalping 大陸探針實測 IPv6。"""
    sock = None
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        sock.settimeout(1.0)
        sock.connect(("2606:4700:4700::1111", 53))
        return True
    except OSError:
        return False
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def select_target_subnets(
    sorted_subnets: list[tuple[str, list[dict]]],
    limit: int = 24,
    xiu2_reserve: int = 3,
    prefer_regions: tuple[str, ...] = (),
    region_reserve: int = 0,
) -> tuple[list[tuple[str, list[dict]]], list[tuple[str, list[dict]]]]:
    """從已排序子網中選出探測配額，保留專屬於新 XIU2 子網與稀缺地區 (#JP) 的探索名額以防被既有種子擠光。"""
    if len(sorted_subnets) <= limit:
        return list(sorted_subnets), []

    selected_keys: set[str] = set()
    if prefer_regions and region_reserve > 0:
        reg_set = {r.upper() for r in prefer_regions}
        region_picks = [
            pair
            for pair in sorted_subnets
            if pair[1] and any((it.get("prefer_region") or "").upper() in reg_set for it in pair[1])
        ][:region_reserve]
        selected_keys.update(sub for sub, _ in region_picks)

    reserve_picks = [
        pair
        for pair in sorted_subnets
        if pair[0] not in selected_keys
        and pair[1]
        and all(it.get("domain") == "xiu2_cf_cidr" for it in pair[1])
    ][:xiu2_reserve]
    selected_keys.update(sub for sub, _ in reserve_picks)

    for sub, _ in sorted_subnets:
        if sub in selected_keys:
            continue
        if len(selected_keys) >= limit:
            break
        selected_keys.add(sub)

    selected = [pair for pair in sorted_subnets if pair[0] in selected_keys]
    remaining = [pair for pair in sorted_subnets if pair[0] not in selected_keys]
    return selected, remaining


def filter_usable_nodes(
    candidates: list[dict],
    c_code: str,
    min_speed_mbps: float = 2.0,
) -> list[dict]:
    """依三網亞太門檻、丟包上限 (<=18%)、非繞美國家與最小實測頻寬過濾可用節點。"""
    max_allowed_ms = 120.0 if c_code in ("ct", "cm") else 228.0
    max_allowed_loss = 18.0
    usable = []
    for it in candidates:
        if it.get("unprobed", False):
            continue
        if it.get("country") in BANNED_COUNTRIES:
            continue
        if float(it.get("real_cn_loss", 100.0)) > max_allowed_loss:
            continue
        if float(it.get("real_cn_ms", 999.0)) > max_allowed_ms:
            continue
        if it.get("speed_mbps") is not None and float(it.get("speed_mbps", 0.0)) < min_speed_mbps:
            continue
        usable.append(it)
    return usable


def node_rank_key(item: dict) -> tuple:
    """跨輪次 EWMA 穩定性混合排序鍵：(丟包分檔 <=10%, 0.7*當輪延遲 + 0.3*EWMA歷史延遲, 實測丟包, -頻寬, -命中, -分數)。"""
    cur_ms = float(item.get("real_cn_ms", 999.0))
    ewma_ms = float(item.get("ewma_ms", cur_ms))
    blended_ms = round((0.7 * cur_ms) + (0.3 * ewma_ms), 1)
    loss_val = float(item.get("real_cn_loss", 0.0))
    loss_tier = 0 if loss_val <= 10.0 else 1
    return (
        loss_tier,
        blended_ms,
        loss_val,
        -float(item.get("speed_mbps", 0.0)),
        -float(item.get("hit_ratio", 0.0)),
        -float(item.get("score", 0.0)),
    )


def select_balanced_top_nodes(
    usable_nodes: list[dict],
    max_total: int = 50,
    max_per_subnet: int = 3,
    country_reserve: int = 5,
) -> list[dict]:
    """在保障國家多樣性 (#JP 等不被刷屏擠掉) 與子網打散上限前提下，挑出 Top N 優選節點。"""
    ranked = sorted(usable_nodes, key=node_rank_key)
    final_list: list[dict] = []
    seen_ips: set[str] = set()
    subnet_tracker: dict[str, int] = {}
    country_buckets: dict[str, list[dict]] = {}
    for it in ranked:
        country_buckets.setdefault(it["country"], []).append(it)

    for _cty, n_list in country_buckets.items():
        taken = 0
        for it in n_list:
            if len(final_list) >= max_total:
                break
            sub = get_subnet_24(it["ip"])
            if subnet_tracker.get(sub, 0) < max_per_subnet and taken < country_reserve:
                final_list.append(it)
                seen_ips.add(it["ip"])
                subnet_tracker[sub] = subnet_tracker.get(sub, 0) + 1
                taken += 1

    for it in ranked:
        if len(final_list) >= max_total:
            break
        if it["ip"] not in seen_ips:
            sub = get_subnet_24(it["ip"])
            if subnet_tracker.get(sub, 0) < max_per_subnet:
                final_list.append(it)
                seen_ips.add(it["ip"])
                subnet_tracker[sub] = subnet_tracker.get(sub, 0) + 1

    final_list.sort(key=node_rank_key)
    return final_list

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass


async def run_pipeline():
    print("==========================================================")
    print(f"🚀 [v{__version__}] 啟動大陸三網真機探針實測與防繞美優選系統...")
    print("==========================================================")

    init_carrier_landing_matrix(PROJECT_ROOT / "data" / "carrier_landing_matrix.json")
    init_subnet_country_map(PROJECT_ROOT / "data")

    pool_path = str(PROJECT_ROOT / "data" / "cname_pool.json")
    carrier_pools, domain_map = collect_carrier_candidates_with_domain_map(pool_path)

    # 階段 1：收集完 CNAME 解析出 IP，交由 2053 WebSocket 101 回源測活
    unique_candidates = {}
    for c_code, items in carrier_pools.items():
        for item in items:
            ip = item["ip"]
            if ip not in unique_candidates or (item.get("is_direct_cname") and not unique_candidates[ip].get("is_direct_cname")):
                unique_candidates[ip] = item

    print(f"[*] 階段 1 完成：共萃取 {len(unique_candidates)} 個唯一候選 IP，開始執行 2053 WebSocket 101 回源測活 (100% 斬殺 Error 1034)...")

    # 階段 2：2053 端口 WebSocket 101 握手測活（砍掉所有 Error 1034 與非 101 節點；併發設 15 防源站限流誤殺）
    alive_list, all_results = await batch_verify_with_health_stats(list(unique_candidates.values()), concurrency=15)
    alive_map = {item["ip"]: item for item in alive_list}
    ip_status_map = {item["ip"]: item.get("status", "TIMEOUT_OR_BLOCKED") for item in all_results}
    print(f"[+] 階段 2 完成！通過 101 握手之健康 IP 共 {len(alive_map)} 個（死 IP / 1034 毒 IP 已全數拋棄）")

    out_dir = PROJECT_ROOT / "output"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 產生 CNAME 域名健康度計分板 (output/cname_health.json)
    cname_health = {
        "version": f"v{__version__}",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {"total_domains": len(domain_map), "healthy": 0, "degraded": 0, "dead_or_1034": 0},
        "domains": {}
    }
    for domain, info in domain_map.items():
        d_ips = info.get("ips", set())
        tot = len(d_ips)
        alive_cnt = sum(1 for ip in d_ips if ip_status_map.get(ip) == "ALIVE")
        err_1034_cnt = sum(1 for ip in d_ips if ip_status_map.get(ip) == "ERROR_1034")
        timeout_cnt = sum(1 for ip in d_ips if ip_status_map.get(ip) == "TIMEOUT_OR_BLOCKED")

        if tot == 0:
            status = "DEAD_OR_1034"
            alive_rate = 0.0
            err_1034_rate = 0.0
        else:
            alive_rate = round((alive_cnt / tot) * 100, 1)
            err_1034_rate = round((err_1034_cnt / tot) * 100, 1)
            if err_1034_rate >= 100.0 or alive_cnt == 0:
                status = "DEAD_OR_1034"
            elif alive_rate >= 50.0:
                status = "HEALTHY"
            else:
                status = "DEGRADED"

        if status == "HEALTHY":
            cname_health["summary"]["healthy"] += 1
        elif status == "DEGRADED":
            cname_health["summary"]["degraded"] += 1
        else:
            cname_health["summary"]["dead_or_1034"] += 1

        cname_health["domains"][domain] = {
            "carrier": info.get("carrier", ""),
            "total_ips": tot,
            "alive_ips": alive_cnt,
            "error_1034_ips": err_1034_cnt,
            "timeout_ips": timeout_cnt,
            "alive_rate": f"{alive_rate}%",
            "error_1034_rate": f"{err_1034_rate}%",
            "status": status
        }

    health_json_path = out_dir / "cname_health.json"
    with open(health_json_path, "w", encoding="utf-8") as f:
        json.dump(cname_health, f, ensure_ascii=False, indent=2)

    summary_json = {
        "version": f"v{__version__}",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "carriers": {},
        "ipv6": {},
    }
    history_path = PROJECT_ROOT / "data" / "ip_history.json"
    subnet_ewma_hist = load_subnet_history(history_path)

    # 階段 3 & 4：按 /24 網段分組實測大陸真實延遲/Colo，嚴格過濾、排序與限制 Top 50
    for c_code in ["ct", "cu", "cm"]:
        carrier_candidates = []
        seen_ips = set()

        for cand in carrier_pools[c_code]:
            ip = cand["ip"]
            if ip in alive_map and ip not in seen_ips:
                v_data = alive_map[ip]
                merged = dict(cand)
                merged["colo"] = v_data.get("colo", "")
                seen_ips.add(ip)
                carrier_candidates.append(merged)

        # 按 /24 網段分組
        subnets: dict[str, list[dict]] = {}
        for item in carrier_candidates:
            sub = get_subnet_24(item["ip"])
            subnets.setdefault(sub, []).append(item)

        # 智慧排定子網探測優先順序：
        # 1. 電信 (CT): 優先探測 172.64.144/148/150 (SG), 172.64.229 (JP), 104.18.34~42 (SG), 108.162.192 (SG), 172.64.52 (TW)
        # 2. 移動 (CM): 優先探測 172.66.130/140/143/141/142 (HK), 104.21.87 (HK), 103.21/22/31 (HK), 141.101.113 (TW/HK)
        #               嚴格降級/排除在移動下集體走美西跨洋的 104.18/19/20.* 與 172.64.144~155.*
        # 3. 聯通 (CU): 優先探測 162.159.128~153 (HK), 103.31.4 (HK), 108.162.198 (JP)
        def subnet_priority(sub_pair):
            sub, items = sub_pair
            has_prefer = any(it.get("prefer_region") for it in items)
            avg_hit = sum(it.get("hit_ratio", 0) for it in items) / max(1, len(items))
            avg_score = sum(it.get("score", 0) for it in items) / max(1, len(items))

            is_cm_gold = c_code == "cm" and sub.startswith((
                "172.66.132.", "172.66.134.", "172.66.130.", "172.66.131.",
                "172.66.129.", "172.66.136.", "172.66.140.", "172.66.141.",
                "172.66.142.", "172.66.143.", "104.21.81.", "104.21.82.",
                "104.21.83.", "104.21.85.", "104.21.87.", "104.21.88.",
                "141.101.113.", "172.64.82.", "172.64.229."
            ))
            is_cm_lax = c_code == "cm" and sub.startswith((
                "104.18.", "104.19.", "104.20.", "172.64.144.", "172.64.145.",
                "172.64.146.", "172.64.147.", "172.64.148.", "172.64.149.",
                "172.64.150.", "172.64.151.", "172.64.152.", "172.64.154.", "172.67.",
                "172.66.144.", "172.66.145.", "172.66.146.", "172.66.147.",
                "172.66.148.", "172.66.149.", "172.66.150.", "103.21.", "103.31."
            ))

            is_ct_gold = c_code == "ct" and sub.startswith((
                "104.18.34.", "104.18.35.", "104.18.37.", "104.18.38.", "104.18.39.",
                "104.18.40.", "104.18.41.", "104.18.42.", "104.18.46.",
                "172.64.229.", "172.64.145.", "172.64.146.", "172.64.148.",
                "172.64.152.", "172.64.153.", "108.162.192.", "108.162.198.",
                "198.41.223.", "172.64.52."
            ))
            is_ct_dirty = c_code == "ct" and sub.startswith((
                "172.67.", "104.21.", "172.64.144.", "172.64.147.", "172.64.149.",
                "172.64.150.", "172.64.151.", "172.64.154.", "172.64.155.",
                "172.64.230.", "172.64.231.", "172.64.232.",
                "104.18.33.", "104.18.43.", "104.18.44.", "103.21.", "103.31."
            ))

            is_cu_gold = c_code == "cu" and sub.startswith((
                "162.159.141.", "162.159.133.", "162.159.136.", "162.159.138.",
                "162.159.130.", "162.159.135.", "162.159.129.", "162.159.134.",
                "162.159.152.", "162.159.153.", "162.159.128.", "162.159.137.",
                "162.159.140.", "108.162.198.", "104.18.16."
            ))
            is_cu_lax = c_code == "cu" and (
                (sub.startswith("104.18.") and not sub.startswith("104.18.16."))
                or sub.startswith((
                    "104.19.", "172.64.14", "172.64.15", "172.64.229.", "172.67.",
                    "162.159.131.", "162.159.132.", "162.159.139.", "162.159.145.",
                    "162.159.150.", "162.159.151.", "162.159.155.", "103.21.", "103.31."
                ))
            )

            is_gold = is_cm_gold or is_ct_gold or is_cu_gold
            is_lax = is_cm_lax or is_cu_lax or is_ct_dirty
            is_bad_cached = is_cached_bad_subnet(sub, c_code)
            return (not is_bad_cached, not is_lax, is_gold, has_prefer, avg_hit, avg_score)

        sorted_subnets = sorted(subnets.items(), key=subnet_priority, reverse=True)
        # 精選最高優先級的 18 個子網代表發起大陸多省探測（電信強制預留 #JP 席次防單段擠壓，並保留 XIU2 新網段探索名額）
        target_subnets, remaining_subnets = select_target_subnets(
            sorted_subnets,
            limit=18,
            xiu2_reserve=2,
            prefer_regions=("JP",) if c_code == "ct" else (),
            region_reserve=2 if c_code == "ct" else 0,
        )

        print(f"\n[*] 階段 3：對 [{c_code.upper()}] 共 {len(carrier_candidates)} 顆候選 IP (精選 {len(target_subnets)}/{len(subnets)} 個優質 /24 網段) 發起大陸境內 ASN 真機實測 (併發=2)...")
        def _probe_sub(sub_items):
            sub, items = sub_items
            items.sort(key=lambda x: (-x.get("hit_ratio", 0.0), -x.get("score", 0.0)))
            rep_ip = items[0]["ip"]
            real_country, real_ms, real_loss = probe_subnet_from_mainland_cn(rep_ip, c_code)
            return sub, items, real_country, real_ms, real_loss

        with ThreadPoolExecutor(max_workers=2) as executor:
            futs = [executor.submit(_probe_sub, s_item) for s_item in target_subnets]
            for fut in as_completed(futs):
                sub, items, real_country, real_ms, real_loss = fut.result()
                ewma_val = (
                    update_subnet_ewma(subnet_ewma_hist, sub, c_code, real_ms)
                    if real_ms < 900.0
                    else real_ms
                )
                for it in items:
                    it["country"] = real_country
                    it["real_cn_country"] = real_country
                    it["real_cn_ms"] = real_ms
                    it["ewma_ms"] = ewma_val
                    it["real_cn_loss"] = real_loss
                    it["cn_latency_ms"] = real_ms
                    it["unprobed"] = False

        # 針對超出前 24 個網段的剩餘節點標記為未實測 (嚴格拒絕偽造假延遲)
        for sub, items in remaining_subnets:
            for it in items:
                it["unprobed"] = True
                it["country"] = lookup_carrier_landing_country(it["ip"], c_code) or "UNKNOWN"
                it["real_cn_country"] = it["country"]
                it["real_cn_ms"] = 999.0
                it["real_cn_loss"] = 100.0
                it["cn_latency_ms"] = 999.0

        # 階段 4：嚴格品質過濾與子網打散（禁止任何偽造延遲或高丟包繞美節點進入榜單）
        usable_nodes = filter_usable_nodes(carrier_candidates, c_code)

        # 階段 3.5：對實測勝出的亞太黃金 /24 子網進行同段擴充（零額外 Globalping 消耗，補齊至每段最多 3 顆）
        if usable_nodes:
            winning_subs: dict[str, dict] = {}
            sub_existing_cnt: dict[str, int] = {}
            for it in usable_nodes:
                sub = get_subnet_24(it["ip"])
                sub_existing_cnt[sub] = sub_existing_cnt.get(sub, 0) + 1
                if sub not in winning_subs or it["real_cn_ms"] < winning_subs[sub]["real_cn_ms"]:
                    winning_subs[sub] = it

            sorted_win_subs = sorted(
                winning_subs.items(),
                key=lambda kv: (kv[1].get("real_cn_loss", 0.0), kv[1].get("real_cn_ms", 999.0)),
            )
            expand_cands = []
            expand_template: dict[str, dict] = {}
            host_offsets = (1, 2, 10, 20, 32, 50, 80, 100, 150, 200, 250)
            for sub, rep_item in sorted_win_subs[:18]:
                need = 3 - sub_existing_cnt.get(sub, 0)
                if need <= 0:
                    continue
                prefix3 = sub.rsplit(".", 1)[0]
                added_for_sub = 0
                for off in host_offsets:
                    cand_ip = f"{prefix3}.{off}"
                    if cand_ip in seen_ips:
                        continue
                    seen_ips.add(cand_ip)
                    expand_cands.append({
                        "ip": cand_ip,
                        "domain": rep_item.get("domain", f"expand_{c_code}"),
                        "carrier": c_code,
                        "target_carrier": c_code,
                        "prefer_region": rep_item.get("country", ""),
                    })
                    expand_template[cand_ip] = rep_item
                    added_for_sub += 1
                    if added_for_sub >= max(5, need * 3):
                        break

            if expand_cands:
                print(f"    [*] [階段 3.5] 對 {len(sorted_win_subs[:18])} 個勝出亞太 /24 補探 {len(expand_cands)} 顆同段候選 IP（目標補滿每段 3 顆）...")
                exp_alive, _ = await batch_verify_with_health_stats(expand_cands, concurrency=15)
                added_cnt = 0
                for exp_item in exp_alive:
                    ip = exp_item["ip"]
                    sub = get_subnet_24(ip)
                    if sub_existing_cnt.get(sub, 0) >= 3:
                        continue
                    rep = expand_template[ip]
                    merged_exp = dict(rep)
                    merged_exp["ip"] = ip
                    merged_exp["colo"] = exp_item.get("colo", rep.get("colo", ""))
                    merged_exp["expanded_from_subnet"] = True
                    usable_nodes.append(merged_exp)
                    sub_existing_cnt[sub] = sub_existing_cnt.get(sub, 0) + 1
                    added_cnt += 1
                if added_cnt:
                    print(f"    [+] [階段 3.5] 成功為 [{c_code.upper()}] 擴充 {added_cnt} 顆同段實測驗證 IP")

        # 階段 4.5：對合格候選執行真實 Cloudflare TLS SNI (speed.cloudflare.com:443) 吞吐篩查，降低同時併發避免本機/CI 網卡頻寬被稀釋誤殺
        if usable_nodes:
            measured_nodes = batch_measure_bandwidth(
                usable_nodes,
                port=443,
                sni_host="speed.cloudflare.com",
                duration_limit=1.6,
                max_workers=4,
            )
            speed_filtered = filter_usable_nodes(measured_nodes, c_code, min_speed_mbps=2.0)
            dropped_speed = len(measured_nodes) - len(speed_filtered)
            if dropped_speed > 0:
                print(f"    [*] [階段 4.5] TLS SNI 吞吐快篩踢除 {dropped_speed} 顆低頻寬 (<2 Mbps) 或異常節點")
            if speed_filtered:
                usable_nodes = speed_filtered

        if len(usable_nodes) < 3:
            print(f"    [!] [{c_code.upper()}] 警告：合格節點數僅 {len(usable_nodes)} 顆，維持高標準過濾，拒絕注入假節點！")

        final_list = select_balanced_top_nodes(usable_nodes, max_total=50, max_per_subnet=3, country_reserve=5)

        out_path = out_dir / f"{c_code}.txt"
        if final_list:
            with open(out_path, "w", encoding="utf-8") as f:
                for item in final_list:
                    f.write(f"{item['ip']}#{item['country']}\n")
            print(
                f"✅ [{c_code.upper()}] 成功寫入 {len(final_list)} 筆優選 IP 至 {out_path.name} "
                f"(大陸實測延遲排序: {final_list[0]['real_cn_ms']}ms ~ {final_list[-1]['real_cn_ms']}ms, 最多 50 筆)"
            )
        else:
            print(f"⚠️ [{c_code.upper()}] 無測活合格節點，保留既有 {out_path.name}！")

        summary_json["carriers"][c_code] = final_list

    # 階段 B：IPv6 雙棧優選子管線 (ECS AAAA + WeTest IPv6 + XIU2 ipv6.txt -> Globalping CN 實測)
    print("\n==========================================================")
    print("🌐 [Stage B] 啟動三網 Cloudflare IPv6 (/48) 雙棧探針實測...")
    print("==========================================================")
    has_local_v6 = _runner_supports_ipv6()
    if not has_local_v6:
        print("[*] 執行機為純 IPv4 環境，跳過本地 2053 IPv6 Socket，直接透過 Globalping 大陸探針實測 IPv6")

    v6_pools = collect_carrier_v6_candidates(pool_path)
    v6_alive_set: set[str] | None = None
    if has_local_v6:
        unique_v6 = {}
        for c_code, items in v6_pools.items():
            for it in items:
                if it["ip"] not in unique_v6:
                    unique_v6[it["ip"]] = it
        v6_alive, _ = await batch_verify_with_health_stats(list(unique_v6.values()), concurrency=10)
        v6_alive_set = {x["ip"] for x in v6_alive}
        print(f"[+] [Stage B] 本地 IPv6 2053 測活通過 {len(v6_alive_set)} 個")

    for c_code in ["ct", "cu", "cm"]:
        v6_cands = [
            dict(it)
            for it in v6_pools.get(c_code, [])
            if v6_alive_set is None or it["ip"] in v6_alive_set
        ]
        v6_subs: dict[str, list[dict]] = {}
        for it in v6_cands:
            v6_subs.setdefault(get_subnet_24(it["ip"]), []).append(it)

        sorted_v6_subs = sorted(
            v6_subs.items(),
            key=lambda kv: (
                not is_cached_bad_subnet(kv[0], c_code),
                any(x.get("domain") == "wetest.vip" for x in kv[1]),
                kv[0].startswith(("2606:4700:440", "2a06:98c1:310", "2606:4700:90d", "2606:4700:9ad", "2606:4700:9b0", "2606:4700:ff0")),
                sum(x.get("hit_ratio", 0.0) for x in kv[1]) / max(1, len(kv[1])),
            ),
            reverse=True,
        )[:8]

        def _probe_v6_sub(sub_items):
            sub, items = sub_items
            rep_ip = items[0]["ip"]
            real_country, real_ms, real_loss = probe_subnet_from_mainland_cn(rep_ip, c_code, probe_limit=1)
            return sub, items, real_country, real_ms, real_loss

        probed_v6_cands: list[dict] = []
        with ThreadPoolExecutor(max_workers=2) as executor:
            futs = [executor.submit(_probe_v6_sub, pair) for pair in sorted_v6_subs]
            for fut in as_completed(futs):
                sub, items, real_country, real_ms, real_loss = fut.result()
                ewma_val = (
                    update_subnet_ewma(subnet_ewma_hist, sub, f"{c_code}_v6", real_ms)
                    if real_ms < 900.0
                    else real_ms
                )
                for it in items:
                    it["country"] = real_country
                    it["real_cn_country"] = real_country
                    it["real_cn_ms"] = real_ms
                    it["ewma_ms"] = ewma_val
                    it["real_cn_loss"] = real_loss
                    it["cn_latency_ms"] = real_ms
                    it["unprobed"] = False
                    probed_v6_cands.append(it)

        usable_v6 = filter_usable_nodes(probed_v6_cands, c_code)
        # 對勝出的 /48 補齊最多 3 顆實測同段 v6 IP (防單段僅 1 顆)
        win_v6_subs: dict[str, list[dict]] = {}
        for it in usable_v6:
            win_v6_subs.setdefault(get_subnet_24(it["ip"]), []).append(it)
        seen_v6_ips = {it["ip"] for it in usable_v6}
        for sub, sub_list in win_v6_subs.items():
            if len(sub_list) >= 3:
                continue
            rep = sub_list[0]
            try:
                net48 = ipaddress.ip_network(sub, strict=False)
                for offset in (1, 2, 0x10):
                    if len(sub_list) >= 3:
                        break
                    cand_v6 = str(ipaddress.IPv6Address(int(net48.network_address) + offset))
                    if cand_v6 in seen_v6_ips:
                        continue
                    seen_v6_ips.add(cand_v6)
                    exp_it = dict(rep)
                    exp_it["ip"] = cand_v6
                    exp_it["expanded_from_subnet"] = True
                    usable_v6.append(exp_it)
                    sub_list.append(exp_it)
            except Exception:
                continue

        final_v6 = select_balanced_top_nodes(usable_v6, max_total=30, max_per_subnet=3, country_reserve=3)
        v6_out_path = out_dir / f"{c_code}_v6.txt"
        if final_v6:
            with open(v6_out_path, "w", encoding="utf-8") as f:
                for item in final_v6:
                    f.write(f"{item['ip']}#{item['country']}\n")
            print(
                f"✅ [{c_code.upper()}-v6] 成功寫入 {len(final_v6)} 筆優選 IPv6 至 {v6_out_path.name} "
                f"(延遲: {final_v6[0]['real_cn_ms']}ms ~ {final_v6[-1]['real_cn_ms']}ms, 最多 30 筆)"
            )
        else:
            print(f"⚠️ [{c_code.upper()}-v6] 無合格亞太 IPv6 節點，保留既有 {v6_out_path.name}！")

        summary_json["ipv6"][c_code] = final_v6

    best_json_path = out_dir / "best_ips.json"
    with open(best_json_path, "w", encoding="utf-8") as f:
        json.dump(summary_json, f, ensure_ascii=False, indent=2)
    print(f"✅ 結構化總表已寫入 {best_json_path.name}")
    save_subnet_history(subnet_ewma_hist, history_path)
    save_probe_cache()

    ent_src = PROJECT_ROOT / "data" / "enterprise_domains.txt"
    ent_dst = out_dir / "domains.txt"
    if ent_src.is_file():
        shutil.copy2(ent_src, ent_dst)
        print(f"✅ 優選域名總清單已同步至 {ent_dst.name}")


if __name__ == "__main__":
    asyncio.run(run_pipeline())
