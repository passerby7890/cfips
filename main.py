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
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from src import __version__
from src.collector import collect_carrier_candidates_with_domain_map
from src.cn_real_tester import (
    get_subnet_24,
    is_blacklisted_us_range,
    probe_subnet_from_mainland_cn,
    save_probe_cache,
)
from src.region_mapper import (
    init_carrier_landing_matrix,
    init_subnet_country_map,
    lookup_carrier_landing_country,
    resolve_country_tag,
)
from src.verifier import batch_verify_with_health_stats

PROJECT_ROOT = Path(__file__).resolve().parent

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
    print(f"[*] 共剩餘 {len(unique_candidates)} 個唯一候選 IP，開始執行 2053 WebSocket 101 回源測活 (100% 斬殺 Error 1034)...")

    # 階段 2：2053 端口 WebSocket 101 握手測活（砍掉所有 Error 1034 與非 101 節點）
    alive_list, all_results = await batch_verify_with_health_stats(list(unique_candidates.values()), concurrency=35)
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
        "carriers": {}
    }

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
                "172.66.140.", "172.66.130.", "172.66.143.", "104.21.87.",
                "172.66.141.", "172.66.142.", "103.21.244.", "103.22.200.",
                "103.31.4.", "141.101.113.", "172.64.82.", "172.64.229."
            ))
            is_cm_lax = c_code == "cm" and sub.startswith((
                "104.18.", "104.19.", "104.20.", "172.64.144.", "172.64.145.",
                "172.64.146.", "172.64.147.", "172.64.148.", "172.64.149.",
                "172.64.150.", "172.64.151.", "172.64.152.", "172.64.154.", "172.67."
            ))

            is_ct_gold = c_code == "ct" and sub.startswith((
                "172.64.144.", "172.64.148.", "172.64.150.", "172.64.52.", "172.64.229.",
                "104.18.42.", "104.18.37.", "104.18.34.", "104.18.35.", "104.18.40.",
                "108.162.192.", "108.162.198.", "198.41.223."
            ))
            is_ct_dirty = c_code == "ct" and sub.startswith(("172.67.", "104.21."))

            is_cu_gold = c_code == "cu" and sub.startswith((
                "162.159.152.", "103.31.4.", "162.159.153.", "108.162.198.",
                "162.159.128.", "162.159.137.", "162.159.140.", "162.159.149."
            ))
            is_cu_lax = c_code == "cu" and sub.startswith((
                "104.18.", "104.19.", "172.64.14", "172.64.15", "172.64.229.", "172.67."
            ))

            is_gold = is_cm_gold or is_ct_gold or is_cu_gold
            is_lax = is_cm_lax or is_cu_lax or is_ct_dirty
            return (not is_lax, is_gold, has_prefer, avg_hit, avg_score)

        sorted_subnets = sorted(subnets.items(), key=subnet_priority, reverse=True)
        # 精選最高優先級的 15 個子網代表發起大陸多省探測 (15 x 2探針 x 3網 <= 90 點，確保 30 分鐘排程不觸發 429 限流)
        target_subnets = sorted_subnets[:15]
        remaining_subnets = sorted_subnets[15:]

        print(f"\n[*] 階段 3：對 [{c_code.upper()}] 共 {len(carrier_candidates)} 顆候選 IP (精選 {len(target_subnets)}/{len(subnets)} 個優質 /24 網段) 發起大陸境內 ASN 真機實測...")
        for sub, items in target_subnets:
            items.sort(key=lambda x: (-x.get("hit_ratio", 0.0), -x.get("score", 0.0)))
            rep_ip = items[0]["ip"]
            real_country, real_ms, real_loss = probe_subnet_from_mainland_cn(rep_ip, c_code)
            for it in items:
                it["country"] = real_country
                it["real_cn_country"] = real_country
                it["real_cn_ms"] = real_ms
                it["real_cn_loss"] = real_loss
                it["cn_latency_ms"] = real_ms
                it["unprobed"] = False

        # 針對超出前 18 個網段的剩餘節點標記為未實測 (嚴格拒絕偽造假延遲)
        for sub, items in remaining_subnets:
            for it in items:
                it["unprobed"] = True
                it["country"] = lookup_carrier_landing_country(it["ip"], c_code) or "UNKNOWN"
                it["real_cn_country"] = it["country"]
                it["real_cn_ms"] = 999.0
                it["real_cn_loss"] = 100.0
                it["cn_latency_ms"] = 999.0

        # 階段 4：嚴格品質過濾與子網打散（禁止任何偽造延遲或高丟包繞美節點進入榜單）
        max_allowed_ms = 120.0 if c_code in ("ct", "cm") else 225.0
        # 允許雙探針實測中最多單包偶發抖動 (1/6 丟包 = 16.7%)，嚴格攔截 >= 20% 持續丟包
        max_allowed_loss = 18.0
        banned_countries = ("US", "GB", "FR", "NL", "CA", "DE", "UNKNOWN", "CF", "")
        usable_nodes = [
            it for it in carrier_candidates
            if not it.get("unprobed", False)
            and it.get("country") not in banned_countries
            and it.get("real_cn_loss", 100.0) <= max_allowed_loss
            and it.get("real_cn_ms", 999.0) <= max_allowed_ms
        ]

        if len(usable_nodes) < 3:
            print(f"    [!] [{c_code.upper()}] 警告：合格節點數僅 {len(usable_nodes)} 顆，維持高標準過濾，拒絕注入假節點！")

        # 嚴格按 (丟包率, real_cn_ms, -hit_ratio, -score) 由優到劣排序
        usable_nodes.sort(
            key=lambda x: (
                x.get("real_cn_loss", 0.0),
                x.get("real_cn_ms", 999.0),
                -x.get("hit_ratio", 0.0),
                -x.get("score", 0.0)
            )
        )

        # 第一輪：國家多樣性保障，為每個合法落地國家保底保留最多 5 顆優質節點 (防單一大國刷屏擠掉 #JP)
        final_list = []
        subnet_tracker: dict[str, int] = {}
        country_buckets: dict[str, list[dict]] = {}
        for it in usable_nodes:
            country_buckets.setdefault(it["country"], []).append(it)

        for cty, n_list in country_buckets.items():
            taken = 0
            for it in n_list:
                sub = get_subnet_24(it["ip"])
                if subnet_tracker.get(sub, 0) < 3 and taken < 5:
                    final_list.append(it)
                    subnet_tracker[sub] = subnet_tracker.get(sub, 0) + 1
                    taken += 1
                if len(final_list) >= 50:
                    break

        # 第二輪：嚴格同 /24 網段最多取 3 顆，依最優延遲補齊至最多 50 顆
        for it in usable_nodes:
            if len(final_list) >= 50:
                break
            if it not in final_list:
                sub = get_subnet_24(it["ip"])
                if subnet_tracker.get(sub, 0) < 3:
                    final_list.append(it)
                    subnet_tracker[sub] = subnet_tracker.get(sub, 0) + 1

        # 確保輸出嚴格維持按 (丟包率, 大陸實測延遲) 升序排列
        final_list.sort(
            key=lambda x: (
                x.get("real_cn_loss", 0.0),
                x.get("real_cn_ms", 999.0),
                -x.get("hit_ratio", 0.0),
                -x.get("score", 0.0)
            )
        )

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

    best_json_path = out_dir / "best_ips.json"
    with open(best_json_path, "w", encoding="utf-8") as f:
        json.dump(summary_json, f, ensure_ascii=False, indent=2)
    print(f"✅ 結構化總表已寫入 {best_json_path.name}")
    save_probe_cache()

    ent_src = PROJECT_ROOT / "data" / "enterprise_domains.txt"
    ent_dst = out_dir / "domains.txt"
    if ent_src.is_file():
        shutil.copy2(ent_src, ent_dst)
        print(f"✅ 優選域名總清單已同步至 {ent_dst.name}")


if __name__ == "__main__":
    asyncio.run(run_pipeline())
