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
from src.collector import collect_carrier_candidates
from src.region_mapper import (
    init_carrier_landing_matrix,
    init_subnet_country_map,
    resolve_country_tag,
)
from src.verifier import batch_verify_unblocked

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

    # 1. 依照三網省級 DNS ECS 解析黃金 CNAME 網域
    pool_path = str(PROJECT_ROOT / "data" / "cname_pool.json")
    carrier_pools = collect_carrier_candidates(pool_path)

    unique_candidates = {}
    for c_code, items in carrier_pools.items():
        for item in items:
            ip = item["ip"]
            if ip not in unique_candidates or (item.get("is_direct_cname") and not unique_candidates[ip].get("is_direct_cname")):
                unique_candidates[ip] = item

    print(f"[*] 共萃取 {len(unique_candidates)} 個唯一候選 IP，開始執行防牆阻斷與邊緣連通性測活...")
    alive_list = await batch_verify_unblocked(list(unique_candidates.values()), concurrency=35)
    alive_map = {item["ip"]: item for item in alive_list}
    print(f"[+] 測活完成！健康未被牆 IP 共 {len(alive_map)} 個（死 IP / GFW 阻斷 IP 已全數拋棄）")

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

        # 排序規則：1. CNAME 原生解析優先；2. 各省命中次數越多越好；3. 延遲越低越好
        verified_carrier_ips.sort(
            key=lambda x: (
                0 if x.get("is_direct_cname") else 1,
                -x.get("hit_count", 1),
                x.get("cn_latency_ms", 999.0)
            )
        )

        # 若該網候選不足 50 個，從其餘未被牆之健康 IP 補充
        if len(verified_carrier_ips) < 50:
            for ip, v_data in alive_map.items():
                if ip not in seen_ips:
                    c_tag = resolve_country_tag(v_data, carrier=c_code)
                    if c_code == "ct" and c_tag not in ("JP", "SG"):
                        continue
                    seen_ips.add(ip)
                    merged = dict(v_data)
                    merged["cn_latency_ms"] = float(v_data.get("cn_latency_ms", 99.0))
                    merged["score"] = float(v_data.get("score", 85.0))
                    merged["colo"] = v_data.get("colo", "")
                    merged["country"] = c_tag
                    verified_carrier_ips.append(merged)
                if len(verified_carrier_ips) >= 50:
                    break

        # 實施「特色 5：同 C 段分散 (抗 QoS)」防護：同一個 /24 網段最多只收錄 3 個 IP
        subnet_counts = {}
        filtered_top50 = []
        for item in verified_carrier_ips:
            if c_code == "ct" and item.get("country") not in ("JP", "SG"):
                continue
            subnet = ".".join(item["ip"].split(".")[:3]) + ".0/24"
            if subnet_counts.get(subnet, 0) < 3:
                subnet_counts[subnet] = subnet_counts.get(subnet, 0) + 1
                filtered_top50.append(item)
            if len(filtered_top50) >= 50:
                break

        # 若經嚴格 /24 限制後候選不足 50 個，從候補中放寬至最多 5 個補齊，確保線路充足
        if len(filtered_top50) < 50:
            for item in verified_carrier_ips:
                if item not in filtered_top50:
                    if c_code == "ct" and item.get("country") not in ("JP", "SG"):
                        continue
                    subnet = ".".join(item["ip"].split(".")[:3]) + ".0/24"
                    if subnet_counts.get(subnet, 0) < 5:
                        subnet_counts[subnet] = subnet_counts.get(subnet, 0) + 1
                        filtered_top50.append(item)
                if len(filtered_top50) >= 50:
                    break

        # 若仍不足 50 筆，無限制放寬同段限制補齊至 50 筆
        if len(filtered_top50) < 50:
            for item in verified_carrier_ips:
                if item not in filtered_top50:
                    if c_code == "ct" and item.get("country") not in ("JP", "SG"):
                        continue
                    filtered_top50.append(item)
                if len(filtered_top50) >= 50:
                    break

        final_top50 = filtered_top50
        out_path = out_dir / f"{c_code}.txt"

        if final_top50:
            with open(out_path, "w", encoding="utf-8") as f:
                for item in final_top50:
                    f.write(f"{item['ip']}#{item['country']}\n")
            direct_cnt = sum(1 for x in final_top50 if x.get("is_direct_cname"))
            print(
                f"✅ [{c_code.upper()}] 成功寫入 {len(final_top50)} 筆優選 IP 至 {out_path.name} "
                f"(含 CNAME 原生 IP {direct_cnt} 筆，已實施 /24 網段分散與 carrier 專屬國家判定)"
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
