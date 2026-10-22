"""CF-Global-QuadNet-Feed: ITDog 同款七大區 ECS 深度解析整合器 (src/collector.py)。

載入 data/cname_pool.json 與 data/cn_ecs_matrix.json，
調用 exhaustive_resolve_domain_for_carrier 榨取三網各省分線與輪詢池的所有極品 IP。
"""

from __future__ import annotations

import ipaddress
import json
import random
from pathlib import Path
from typing import Dict, List

from src.dns_extractor import exhaustive_resolve_domain_for_carrier
from src.region_mapper import lookup_carrier_landing_country


def collect_carrier_candidates_with_domain_map(
    pool_path: str = "data/cname_pool.json",
    matrix_path: str = "data/cn_ecs_matrix.json"
) -> Tuple[Dict[str, List[Dict]], Dict[str, Dict[str, Any]]]:
    with open(pool_path, "r", encoding="utf-8") as f:
        pool_cfg = json.load(f)
    with open(matrix_path, "r", encoding="utf-8") as f:
        ecs_matrix = json.load(f)

    # 讀取 carrier_landing_matrix 作為亞太直連擴充庫
    carrier_landing_cfg = {}
    matrix_file = Path(pool_path).resolve().parent / "carrier_landing_matrix.json"
    if matrix_file.is_file():
        try:
            with open(matrix_file, "r", encoding="utf-8") as mf:
                carrier_landing_cfg = json.load(mf).get("carriers", {})
        except Exception:
            pass

    carriers_cfg = pool_cfg.get("carriers", {})
    carrier_results: Dict[str, List[Dict]] = {"ct": [], "cu": [], "cm": []}
    domain_map: Dict[str, Dict[str, Any]] = {}

    for c_code in ["ct", "cu", "cm"]:
        c_domains = carriers_cfg.get(c_code, [])
        c_anchors = ecs_matrix.get(c_code, [])
        direct_map: Dict[str, Dict] = {}

        print(f"\n[*] [@Mainland_Probe] 啟動 ITDog 同款七大區 ECS 深度解析 [{c_code.upper()}] (共 {len(c_domains)} 個網域 x {len(c_anchors)} 省錨點)...")

        for d_info in c_domains:
            domain = d_info["domain"]
            prefer_reg = (d_info.get("prefer_region") or "").strip().upper()
            if domain not in domain_map:
                domain_map[domain] = {
                    "carrier": c_code,
                    "ips": set(),
                    "cn_latency_ms": float(d_info.get("cn_latency_ms", 999.0)),
                    "prefer_region": prefer_reg
                }
            extracted = exhaustive_resolve_domain_for_carrier(domain, c_anchors, rounds=2)
            ip_summary = ", ".join([f"{x['ip']}({x['hit_ratio']}%)" for x in extracted[:6]])
            print(f"    -> [{c_code.upper()}] {domain} (prefer: {prefer_reg or 'AUTO'}) 榨出 {len(extracted)} 個唯一 IP: {ip_summary}")

            for item in extracted:
                ip = item["ip"]
                domain_map[domain]["ips"].add(ip)

                # 過濾私有/無效 IP（所有公網 Anycast IP 均保留並交由真機探針以真實 RTT 淘汰）
                from src.cn_real_tester import is_blacklisted_us_range
                if is_blacklisted_us_range(ip, c_code):
                    continue

                lat_val = float(d_info.get("cn_latency_ms", 999.0))
                if ip not in direct_map:
                    direct_map[ip] = {
                        "ip": ip,
                        "domain": domain,
                        "carrier": c_code,
                        "target_carrier": c_code,
                        "cn_latency_ms": lat_val,
                        "speed_mbs": float(d_info.get("speed_mbs", 12.0)),
                        "score": float(d_info.get("score", 90.0)),
                        "hit_ratio": item["hit_ratio"],
                        "prefer_region": prefer_reg,
                        "is_direct_cname": True
                    }
                else:
                    # 若多個優選網域都指向同一顆好 IP，累加其命中權重並取最低延遲
                    direct_map[ip]["hit_ratio"] = round(direct_map[ip]["hit_ratio"] + item["hit_ratio"], 2)
                    if lat_val < direct_map[ip]["cn_latency_ms"]:
                        direct_map[ip]["cn_latency_ms"] = lat_val
                    if prefer_reg and not direct_map[ip].get("prefer_region"):
                        direct_map[ip]["prefer_region"] = prefer_reg

        # 按：1. 國內實測延遲由低到高；2. 跨省解析命中率 (hit_ratio) 由高到低；3. 評分由高到低
        direct_list = sorted(
            direct_map.values(),
            key=lambda x: (x["cn_latency_ms"], -x["hit_ratio"], -x["score"])
        )

        # 依用戶指示：只針對 DNS 萃取出來的優選 IP 做紀錄並排序，不做任何延伸 (不補鄰居、不補外部矩陣)
        carrier_results[c_code] = direct_list

    return carrier_results, domain_map


def collect_carrier_candidates(
    pool_path: str = "data/cname_pool.json",
    matrix_path: str = "data/cn_ecs_matrix.json"
) -> Dict[str, List[Dict]]:
    results, _ = collect_carrier_candidates_with_domain_map(pool_path, matrix_path)
    return results
