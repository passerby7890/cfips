#!/usr/bin/env python3
"""CF-Global-QuadNet-Feed 主控流程 (v3.0.0 三網黃金 CNAME 域名解析與 101 測活系統)。

架構規範：
1. 透過 data/cname_pool.json 解析三網 (CT, CU, CM) 黃金 CNAME 網域與 ECS 錨點。
2. 對萃取出的獨立 IP 執行防牆阻斷、/cdn-cgi/trace 與 :2053/ws 101 回源握手測活。
3. CNAME 原生解析 IP 永遠優先排在最前面，再依國內 24H 實測延遲由小到大排序。
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
from src.collector import collect_carrier_candidates
from src.region_mapper import init_subnet_country_map
from src.verifier import batch_verify_101

PROJECT_ROOT = Path(__file__).resolve().parent

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass


async def run_pipeline():
    print("==========================================================")
    print(f"🚀 [v{__version__}] 啟動三網黃金 CNAME 域名解析與 101 測活系統...")
    print("==========================================================")

    # 初始化網段國家對照庫
    init_subnet_country_map(PROJECT_ROOT / "data")

    # 1. 解析三網黃金 CNAME 網域
    pool_path = str(PROJECT_ROOT / "data" / "cname_pool.json")
    carrier_pools = collect_carrier_candidates(pool_path)

    unique_candidates = {}
    for c_code, items in carrier_pools.items():
        for item in items:
            ip = item["ip"]
            if ip not in unique_candidates or (item.get("is_direct_cname") and not unique_candidates[ip].get("is_direct_cname")):
                unique_candidates[ip] = item

    print(f"[*] 共萃取 {len(unique_candidates)} 個唯一候選 IP，開始執行防牆與 :2053/ws 101 測活...")
    alive_list = await batch_verify_101(list(unique_candidates.values()), concurrency=30)
    alive_map = {item["ip"]: item for item in alive_list}
    print(f"[+] 測活完成！通過 101 握手之健康 IP 共 {len(alive_map)} 個（死 IP / 被牆 IP 已全數拋棄）")

    out_dir = PROJECT_ROOT / "output"
    out_dir.mkdir(parents=True, exist_ok=True)

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
                seen_ips.add(ip)
                v_data = alive_map[ip]
                merged = dict(cand)
                merged["country"] = v_data.get("country", "HK")
                merged["colo"] = v_data.get("colo", "")
                verified_carrier_ips.append(merged)

        # 排序規則：1. CNAME 原生直接解析出的 IP 優先；2. 國內 24H 實測延遲由低到高；3. 評分由高到低
        verified_carrier_ips.sort(
            key=lambda x: (0 if x.get("is_direct_cname") else 1, x.get("cn_latency_ms", 999.0), -x.get("score", 0.0))
        )

        # 若該網候選不足 50 個，從其餘通過 101 握手之健康 IP 補充
        if len(verified_carrier_ips) < 50:
            for ip, v_data in alive_map.items():
                if ip not in seen_ips:
                    seen_ips.add(ip)
                    merged = dict(v_data)
                    merged["cn_latency_ms"] = float(v_data.get("cn_latency_ms", 99.0))
                    merged["score"] = float(v_data.get("score", 85.0))
                    verified_carrier_ips.append(merged)
                if len(verified_carrier_ips) >= 50:
                    break

        # 截取前 50 名輸出至 output/{ct,cu,cm}.txt
        final_top50 = verified_carrier_ips[:50]
        out_path = out_dir / f"{c_code}.txt"

        if final_top50:
            with open(out_path, "w", encoding="utf-8") as f:
                for item in final_top50:
                    f.write(f"{item['ip']}#{item['country']}\n")
            direct_cnt = sum(1 for x in final_top50 if x.get("is_direct_cname"))
            print(
                f"✅ [{c_code.upper()}] 成功寫入 {len(final_top50)} 筆優選 IP 至 {out_path.name} "
                f"(含 CNAME 原生 IP {direct_cnt} 筆，最低國內延遲: {final_top50[0]['cn_latency_ms']}ms)"
            )
        else:
            print(f"⚠️ [{c_code.upper()}] 當次無測活通過節點，保留既有 {out_path.name} 快照！")

        summary_json["carriers"][c_code] = final_top50

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
