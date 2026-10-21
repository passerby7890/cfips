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


def collect_carrier_candidates(
    pool_path: str = "data/cname_pool.json",
    matrix_path: str = "data/cn_ecs_matrix.json"
) -> Dict[str, List[Dict]]:
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

    for c_code in ["ct", "cu", "cm"]:
        c_domains = carriers_cfg.get(c_code, [])
        c_anchors = ecs_matrix.get(c_code, [])
        direct_map: Dict[str, Dict] = {}

        print(f"\n[*] [@Mainland_Probe] 啟動 ITDog 同款七大區 ECS 深度解析 [{c_code.upper()}] (共 {len(c_domains)} 個網域 x {len(c_anchors)} 省錨點)...")

        for d_info in c_domains:
            domain = d_info["domain"]
            extracted = exhaustive_resolve_domain_for_carrier(domain, c_anchors, rounds=2)
            ip_summary = ", ".join([f"{x['ip']}({x['hit_ratio']}%)" for x in extracted[:6]])
            print(f"    -> [{c_code.upper()}] {domain} 榨出 {len(extracted)} 個唯一 IP: {ip_summary}")

            for item in extracted:
                ip = item["ip"]
                # 電信 (CT) 嚴格遵循落地規則：僅收錄落地為新加坡 (SG) 與日本 (JP) 之節點
                if c_code == "ct":
                    landing = lookup_carrier_landing_country(ip, c_code)
                    if landing not in ("JP", "SG"):
                        continue

                if ip not in direct_map:
                    direct_map[ip] = {
                        "ip": ip,
                        "domain": domain,
                        "carrier": c_code,
                        "target_carrier": c_code,
                        "cn_latency_ms": float(d_info["cn_latency_ms"]),
                        "speed_mbs": float(d_info.get("speed_mbs", 12.0)),
                        "score": float(d_info.get("score", 90.0)),
                        "hit_ratio": item["hit_ratio"],
                        "is_direct_cname": True
                    }
                else:
                    # 若多個優選網域都指向同一顆好 IP，累加其命中權重並取最低延遲
                    direct_map[ip]["hit_ratio"] = round(direct_map[ip]["hit_ratio"] + item["hit_ratio"], 2)
                    if float(d_info["cn_latency_ms"]) < direct_map[ip]["cn_latency_ms"]:
                        direct_map[ip]["cn_latency_ms"] = float(d_info["cn_latency_ms"])

        # 按：1. 國內實測延遲由低到高；2. 跨省解析命中率 (hit_ratio) 由高到低；3. 評分由高到低
        direct_list = sorted(
            direct_map.values(),
            key=lambda x: (x["cn_latency_ms"], -x["hit_ratio"], -x["score"])
        )

        # 若該網所有 CNAME 榨出的唯一 IP 總數不足 75 個，從排名前茅的黃金 /24 補齊少量鄰居
        neighbor_list = []
        seen_all = set(direct_map.keys())
        for item in direct_list:
            if len(direct_list) + len(neighbor_list) >= 75:
                break
            try:
                net = ipaddress.IPv4Network(f"{item['ip']}/24", strict=False)
                for h_idx in random.sample(range(2, 253), 2):
                    n_ip = str(net[h_idx])
                    if n_ip not in seen_all:
                        if c_code == "ct":
                            n_landing = lookup_carrier_landing_country(n_ip, c_code)
                            if n_landing not in ("JP", "SG"):
                                continue
                        seen_all.add(n_ip)
                        neighbor_list.append({
                            "ip": n_ip,
                            "domain": f"{item['domain']}(/24同段)",
                            "carrier": c_code,
                            "target_carrier": c_code,
                            "cn_latency_ms": round(item["cn_latency_ms"] + 2.0, 1),
                            "speed_mbs": item["speed_mbs"],
                            "score": item["score"] - 1.0,
                            "hit_ratio": 1.0,
                            "is_direct_cname": False
                        })
            except Exception:
                continue

        # 從 carrier_landing_matrix.json 的專屬網段精選補充多樣化子網 (確保 50 筆名額充足且高分散)
        matrix_seeds = []
        if c_code in ("ct", "cu", "cm"):
            c_rules = carrier_landing_cfg.get(c_code, {}).get("landing_rules", {})
            for ctry, cidrs in c_rules.items():
                for cidr in cidrs:
                    if len(direct_list) + len(neighbor_list) + len(matrix_seeds) >= 120:
                        break
                    try:
                        c_net = ipaddress.ip_network(cidr.strip(), strict=False)
                        sub_net = list(c_net.subnets(new_prefix=24))[0] if c_net.prefixlen < 24 else c_net
                        for host_idx in [12, 28, 46]:
                            m_ip = str(sub_net[host_idx])
                            if m_ip not in seen_all:
                                seen_all.add(m_ip)
                                matrix_seeds.append({
                                    "ip": m_ip,
                                    "domain": f"matrix.{ctry.lower()}.cloudflare",
                                    "carrier": c_code,
                                    "target_carrier": c_code,
                                    "cn_latency_ms": 45.0 if ctry in ("HK", "JP", "SG") else 145.0,
                                    "speed_mbs": 13.0,
                                    "score": 92.0,
                                    "hit_ratio": 1.0,
                                    "is_direct_cname": False
                                })
                    except Exception:
                        continue

        carrier_results[c_code] = direct_list + neighbor_list + matrix_seeds

    return carrier_results
