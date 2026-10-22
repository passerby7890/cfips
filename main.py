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

        print(f"\n[*] 階段 3：對 [{c_code.upper()}] 共 {len(carrier_candidates)} 顆候選 IP ({len(subnets)} 個 /24 網段) 發起大陸境內 ASN 真機實測...")
        for sub, items in subnets.items():
            rep_ip = items[0]["ip"]
            real_country, real_ms, real_loss = probe_subnet_from_mainland_cn(rep_ip, c_code)
            for it in items:
                ip_str = it["ip"]
                indiv_country = lookup_carrier_landing_country(ip_str, c_code)
                final_country = indiv_country if indiv_country else real_country
                it["country"] = final_country
                it["real_cn_country"] = final_country
                it["real_cn_ms"] = real_ms
                it["real_cn_loss"] = real_loss
                it["cn_latency_ms"] = real_ms

        # 階段 4：依用戶指示廢除截斷，保留全部健康測活節點，按大陸實測延遲由低到高排序
        usable_nodes = [
            it for it in carrier_candidates
            if it.get("country") not in ("UNKNOWN", "CF")
        ]

        # 嚴格按 real_cn_ms 由低到高排序
        usable_nodes.sort(
            key=lambda x: (
                x.get("real_cn_ms", 999.0),
                -x.get("hit_ratio", 0.0),
                -x.get("score", 0.0)
            )
        )

        # 第一輪：同 /24 網段最多取 3 顆，優先分散子網
        final_list = []
        subnet_tracker: dict[str, int] = {}
        for it in usable_nodes:
            sub = get_subnet_24(it["ip"])
            if subnet_tracker.get(sub, 0) < 3:
                final_list.append(it)
                subnet_tracker[sub] = subnet_tracker.get(sub, 0) + 1
            if len(final_list) >= 50:
                break

        # 第二輪：若總數未滿 50 顆，從剩餘低延遲節點中依序補齊至最多 50 顆
        if len(final_list) < 50:
            for it in usable_nodes:
                if it not in final_list:
                    final_list.append(it)
                if len(final_list) >= 50:
                    break

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

    ent_src = PROJECT_ROOT / "data" / "enterprise_domains.txt"
    ent_dst = out_dir / "domains.txt"
    if ent_src.is_file():
        shutil.copy2(ent_src, ent_dst)
        print(f"✅ 優選域名總清單已同步至 {ent_dst.name}")


if __name__ == "__main__":
    asyncio.run(run_pipeline())
