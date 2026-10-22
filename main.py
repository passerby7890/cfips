#!/usr/bin/env python3
"""CF-Global-QuadNet-Feed 主控流程 (v3.0.0 三網省級 DNS 解析與防牆測活系統)。

架構規範：
1. 透過 data/cname_pool.json 依照三網省級 DNS ECS 解析三網 (CT, CU, CM) 黃金 CNAME 網域。
2. 對萃取出的獨立 IP 執行防牆阻斷檢測與 /cdn-cgi/trace 機房識別。
3. 統計省級解析命中權重，CNAME 原生高頻解析 IP 優先排序。
4. 寫入 output/ct.txt, output/cu.txt, output/cm.txt, output/domains.txt 及 output/best_ips.json。
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
from src.region_mapper import (
    init_carrier_landing_matrix,
    init_subnet_country_map,
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
    print(f"🚀 [v{__version__}] 啟動三網省級 DNS 解析與防牆測活系統...")
    print("==========================================================")

    # 初始化三網物理落地矩陣與網段國家對照庫
    init_carrier_landing_matrix(PROJECT_ROOT / "data" / "carrier_landing_matrix.json")
    init_subnet_country_map(PROJECT_ROOT / "data")

    # 1. 依照三網省級 DNS ECS 解析黃金 CNAME 網域 (同步取得網域解析 IP 關聯表)
    pool_path = str(PROJECT_ROOT / "data" / "cname_pool.json")
    carrier_pools, domain_map = collect_carrier_candidates_with_domain_map(pool_path)

    unique_candidates = {}
    for c_code, items in carrier_pools.items():
        for item in items:
            ip = item["ip"]
            if ip not in unique_candidates or (item.get("is_direct_cname") and not unique_candidates[ip].get("is_direct_cname")):
                unique_candidates[ip] = item

    print(f"[*] 共萃取 {len(unique_candidates)} 個唯一候選 IP，開始執行防牆阻斷與邊緣連通性測活...")
    alive_list, all_results = await batch_verify_with_health_stats(list(unique_candidates.values()), concurrency=35)
    alive_map = {item["ip"]: item for item in alive_list}
    ip_status_map = {item["ip"]: item.get("status", "TIMEOUT_OR_BLOCKED") for item in all_results}
    print(f"[+] 測活完成！健康未被牆 IP 共 {len(alive_map)} 個（死 IP / GFW 阻斷 IP 已全數拋棄）")

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
    print(
        f"✅ CNAME 域名健康度計分板已寫入 {health_json_path.name} "
        f"(健康: {cname_health['summary']['healthy']}, 降級: {cname_health['summary']['degraded']}, 阻斷/失效: {cname_health['summary']['dead_or_1034']})"
    )

    summary_json = {
        "version": f"v{__version__}",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "carriers": {}
    }

    for c_code in ["ct", "cu", "cm"]:
        verified_carrier_ips = []
        seen_ips = set()

        for cand in carrier_pools[c_code]:
            ip = cand["ip"]
            if ip in alive_map and ip not in seen_ips:
                v_data = alive_map[ip]
                merged = dict(cand)
                merged["colo"] = v_data.get("colo", "")
                c_tag = resolve_country_tag(merged, runner_trace_colo=merged["colo"], carrier=c_code)
                # 電信 (CT) 嚴格保證僅落地新加坡 (SG) 與日本 (JP)
                if c_code == "ct" and c_tag not in ("JP", "SG"):
                    continue
                seen_ips.add(ip)
                merged["country"] = c_tag
                verified_carrier_ips.append(merged)

        # 排序規則：1. 國內實測延遲由低到高；2. 跨省解析命中率 (hit_ratio) 由高到低；3. 評分由高到低
        verified_carrier_ips.sort(
            key=lambda x: (
                x.get("cn_latency_ms", 999.0),
                -x.get("hit_ratio", 0.0),
                -x.get("score", 0.0)
            )
        )

        # 依用戶指示：只針對 DNS 萃取出來的優選 IP 做紀錄，標示位置並排序，不做延伸與強行補位
        final_list = verified_carrier_ips
        out_path = out_dir / f"{c_code}.txt"

        if final_list:
            with open(out_path, "w", encoding="utf-8") as f:
                for item in final_list:
                    f.write(f"{item['ip']}#{item['country']}\n")
            print(
                f"✅ [{c_code.upper()}] 成功寫入 {len(final_list)} 筆純 DNS 萃取優選 IP 至 {out_path.name} "
                f"(純淨無延伸，已依延遲與權重排序並標註落地國家)"
            )
        else:
            print(f"⚠️ [{c_code.upper()}] 當次無測活通過節點，保留既有 {out_path.name} 快照！")

        summary_json["carriers"][c_code] = final_list

    # 輸出結構化 JSON 總表
    best_json_path = out_dir / "best_ips.json"
    with open(best_json_path, "w", encoding="utf-8") as f:
        json.dump(summary_json, f, ensure_ascii=False, indent=2)
    print(f"✅ 結構化總表已寫入 {best_json_path.name}")

    # 同步優選域名清單至 output/domains.txt
    ent_src = PROJECT_ROOT / "data" / "enterprise_domains.txt"
    ent_dst = out_dir / "domains.txt"
    if ent_src.is_file():
        shutil.copy2(ent_src, ent_dst)
        print(f"✅ 優選域名總清單已同步至 {ent_dst.name}")


if __name__ == "__main__":
    asyncio.run(run_pipeline())
