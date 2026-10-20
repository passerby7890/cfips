"""CF-Global-QuadNet-Feed: 多路 DoH + ECS 三網黃金 CNAME 域名解析器 (v3.0.0)。"""

from __future__ import annotations

import ipaddress
import json
import random
import re
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/dns-json",
}

DOH_ENDPOINTS = [
    "https://dns.google/resolve",
    "https://dns.alidns.com/resolve",
    "https://doh.pub/resolve",
]

IPV4_REGEX_VALID = lambda s: len(s.split(".")) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in s.split("."))

# 全域連線池加速
_GLOBAL_SESSION = requests.Session()
_GLOBAL_SESSION.headers.update(HEADERS)


def _query_doh_single(doh_url: str, domain: str, anchor: str) -> List[str]:
    try:
        params = {
            "name": domain,
            "type": "A",
            "edns_client_subnet": f"{anchor}/24"
        }
        r = _GLOBAL_SESSION.get(doh_url, params=params, timeout=2.0)
        if r.status_code == 200:
            data = r.json()
            ips = []
            for ans in data.get("Answer", []):
                if ans.get("type") == 1:
                    ip = ans.get("data", "").strip()
                    if IPV4_REGEX_VALID(ip) and not ip.startswith("127."):
                        ips.append(ip)
            return ips
    except Exception:
        pass
    return []


def resolve_domain_with_ecs(domain: str, ecs_ips: List[str]) -> List[str]:
    """透過多路 DoH (帶三網 ECS 子網) 與系統 DNS，徹底榨出 CNAME 背後的所有優選 IP"""
    found_ips = []
    seen = set()

    # 1. 併發多路 DoH + 北上廣三網 ECS 解析
    tasks = [(doh_url, domain, anchor) for doh_url in DOH_ENDPOINTS for anchor in ecs_ips]
    with ThreadPoolExecutor(max_workers=len(tasks) or 5) as executor:
        futs = [executor.submit(_query_doh_single, u, d, a) for u, d, a in tasks]
        for f in as_completed(futs):
            for ip in f.result():
                if ip not in seen:
                    seen.add(ip)
                    found_ips.append(ip)

    # 2. 系統原生 DNS 解析兜底
    try:
        infos = socket.getaddrinfo(domain, None, socket.AF_INET)
        for info in infos:
            ip = info[4][0]
            if IPV4_REGEX_VALID(ip) and ip not in seen and not ip.startswith("127."):
                seen.add(ip)
                found_ips.append(ip)
    except Exception:
        pass

    return found_ips


def collect_carrier_candidates(config_path: str = "data/cname_pool.json") -> Dict[str, List[Dict]]:
    """平行解析三網黃金 CNAME 網域清單，提取所有候選 IP"""
    with open(config_path, "r", encoding="utf-8") as f:
        pool_cfg = json.load(f)

    ecs_anchors = pool_cfg.get("ecs_anchors", {})
    carriers_cfg = pool_cfg.get("carriers", {})
    carrier_results: Dict[str, List[Dict]] = {"ct": [], "cu": [], "cm": []}

    for c_code in ["ct", "cu", "cm"]:
        c_domains = carriers_cfg.get(c_code, [])
        anchors = ecs_anchors.get(c_code, ["183.47.126.35"])
        direct_map: Dict[str, Dict] = {}

        print(f"[*] [@Mainland_Probe] 正在透過三網 ECS 解析 [{c_code.upper()}] 的 {len(c_domains)} 個黃金 CNAME 網域...", flush=True)

        with ThreadPoolExecutor(max_workers=15) as executor:
            future_map = {
                executor.submit(resolve_domain_with_ecs, item["domain"], anchors): item
                for item in c_domains
            }
            for fut in as_completed(future_map):
                d_info = future_map[fut]
                domain_name = d_info["domain"]
                try:
                    ips = fut.result()
                except Exception:
                    ips = []
                print(f"    -> {domain_name} 解析出 {len(ips)} 個 IP", flush=True)
                for ip in ips:
                    if ip not in direct_map or d_info["cn_latency_ms"] < direct_map[ip]["cn_latency_ms"]:
                        direct_map[ip] = {
                            "ip": ip,
                            "domain": domain_name,
                            "cn_latency_ms": float(d_info["cn_latency_ms"]),
                            "speed_mbs": float(d_info.get("speed_mbs", 12.0)),
                            "score": float(d_info.get("score", 90.0)),
                            "is_direct_cname": True
                        }

        # 先將所有 CNAME 原生解析出的 IP 按延遲與評分排序
        direct_list = sorted(
            direct_map.values(),
            key=lambda x: (x["cn_latency_ms"], -x["score"])
        )

        # 若原生解析 IP 數量不足，從已解析出的低延遲 CNAME IP 同 /24 網段抽取鄰居作為備援候選 (確保測活後湊滿 50 筆)
        neighbor_list = []
        seen_all = set(direct_map.keys())
        for item in direct_list:
            if len(direct_list) + len(neighbor_list) >= 110:
                break
            try:
                net = ipaddress.IPv4Network(f"{item['ip']}/24", strict=False)
                sampled_hosts = random.sample(range(2, 253), 6)
                for h_idx in sampled_hosts:
                    n_ip = str(net[h_idx])
                    if n_ip not in seen_all:
                        seen_all.add(n_ip)
                        neighbor_list.append({
                            "ip": n_ip,
                            "domain": f"{item['domain']}(/24鄰居)",
                            "cn_latency_ms": round(item["cn_latency_ms"] + 2.0, 1),
                            "speed_mbs": item["speed_mbs"],
                            "score": item["score"] - 1.0,
                            "is_direct_cname": False
                        })
            except Exception:
                continue

        carrier_results[c_code] = direct_list + neighbor_list

    return carrier_results
