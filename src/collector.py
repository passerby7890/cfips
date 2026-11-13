"""CF-Global-QuadNet-Feed: 三網深度候選收集與多源融合引擎 (src/collector.py, v4.6.0)。

功能：
1. 載入 data/cname_pool.json 與 data/cn_ecs_matrix.json，調用 exhaustive_resolve_domain_for_carrier 萃取三網 CNAME 解析 IP。
2. 融合各運營商專屬精選種子庫 (data/seeds_cm.txt, data/seeds_ct.txt, data/seeds_cu.txt)，納入實測 sub-100ms 亞太直連段。
3. 整合境內外部即時測速源 (WeTest / HostMonit / uouin)，注入真實探測觀測值並拓展候選池。
4. 全數交由後續 2053 WebSocket 101 回源測活與境內真機探針嚴格驗收。
"""

from __future__ import annotations

import ipaddress
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple
import requests

from src.cn_real_tester import record_real_probe_observation
from src.dns_extractor import exhaustive_resolve_domain_for_carrier
from src.region_mapper import is_cf_official_anycast


def _load_seeds_for_carrier(seed_path: Path) -> List[str]:
    """從 seeds 檔案載入單 IP 與 CIDR 網段採樣代表 IP"""
    if not seed_path.is_file():
        return []
    ips: List[str] = []
    try:
        with open(seed_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                target = line.split("#")[0].strip()
                if "/" in target:
                    try:
                        net = ipaddress.ip_network(target, strict=False)
                        # 對 CIDR 網段抽取有代表性的主機
                        sample_hosts = [1, 2, 10, 20, 32, 50, 80, 100, 150, 200, 250]
                        for h in sample_hosts:
                            if h < net.num_addresses - 1:
                                ips.append(str(net[h]))
                    except Exception:
                        pass
                elif re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", target):
                    ips.append(target)
    except Exception:
        pass
    return list(dict.fromkeys(ips))


def _fetch_external_feed_ips() -> Dict[str, List[Dict[str, Any]]]:
    """快速抓取外部境內測速平台的真實優選 IP (Fail-open，超時 3.0s)"""
    results: Dict[str, List[Dict[str, Any]]] = {"ct": [], "cu": [], "cm": []}

    # 1. WeTest 即時測速數據庫
    try:
        r = requests.get("https://www.wetest.vip/page/cloudflare/address_v4.html", timeout=3.0)
        if r.status_code == 200:
            rows = re.findall(r"<tr[^>]*>(.*?)</tr>", r.text, re.S)
            for row in rows:
                cols = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
                if len(cols) >= 6:
                    line_name = cols[0]
                    ip = cols[1]
                    ms_str = cols[4]
                    colo = cols[5].upper()
                    m_ms = re.search(r"(\d+)", ms_str)
                    ms = float(m_ms.group(1)) if m_ms else 150.0
                    c_code = "cm" if "移动" in line_name else ("cu" if "联通" in line_name else ("ct" if "电信" in line_name else ""))
                    if c_code and is_cf_official_anycast(ip):
                        country = "HK" if colo in ("HKG", "MFM") else ("SG" if colo == "SIN" else ("JP" if colo in ("NRT", "KIX") else ("TW" if colo in ("TPE", "KHH") else ("US" if colo in ("LAX", "SJC", "SEA") else ""))))
                        if country:
                            record_real_probe_observation(ip, c_code, country, ms, 0.0, source="wetest")
                        results[c_code].append({
                            "ip": ip,
                            "domain": "wetest.vip",
                            "cn_latency_ms": ms,
                            "colo": colo,
                            "hit_ratio": 95.0,
                            "score": 95.0
                        })
    except Exception:
        pass

    # 2. HostMonit 境內三網測速 API
    try:
        r_hm = requests.post("https://api.hostmonit.com/get_optimization_ip", json={"key": "iDetkOys"}, timeout=3.0)
        if r_hm.status_code == 200:
            hm_data = r_hm.json().get("info", [])
            for item in hm_data:
                ip = item.get("ip", "")
                c_str = str(item.get("line", "")).lower()
                lat = float(item.get("latency", 150.0))
                loss = float(item.get("loss", 0.0))
                c_code = "cm" if c_str == "cm" else ("cu" if c_str == "cu" else ("ct" if c_str == "ct" else ""))
                if c_code and is_cf_official_anycast(ip):
                    results[c_code].append({
                        "ip": ip,
                        "domain": "api.hostmonit.com",
                        "cn_latency_ms": lat,
                        "colo": item.get("colo", ""),
                        "hit_ratio": 90.0,
                        "score": 90.0
                    })
    except Exception:
        pass

    return results


def collect_carrier_candidates_with_domain_map(
    pool_path: str = "data/cname_pool.json",
    matrix_path: str = "data/cn_ecs_matrix.json"
) -> Tuple[Dict[str, List[Dict]], Dict[str, Dict[str, Any]]]:
    pool_file = Path(pool_path).resolve()
    with open(pool_file, "r", encoding="utf-8") as f:
        pool_cfg = json.load(f)
    matrix_file_ecs = Path(matrix_path)
    if not matrix_file_ecs.is_file():
        matrix_file_ecs = pool_file.parent / matrix_file_ecs.name
    with open(matrix_file_ecs, "r", encoding="utf-8") as f:
        ecs_matrix = json.load(f)

    data_dir = pool_file.parent
    carriers_cfg = pool_cfg.get("carriers", {})
    carrier_results: Dict[str, List[Dict]] = {"ct": [], "cu": [], "cm": []}
    domain_map: Dict[str, Dict[str, Any]] = {}

    # 1. 抓取外部即時測速源候選
    print("[*] 正在同步境內權威實測數據源 (WeTest / HostMonit)...")
    external_candidates = _fetch_external_feed_ips()
    for c_code, ext_list in external_candidates.items():
        if ext_list:
            print(f"    -> [{c_code.upper()}] 成功納入 {len(ext_list)} 顆外部境內實測熱門 IP")

    for c_code in ["ct", "cu", "cm"]:
        c_domains = carriers_cfg.get(c_code, [])
        c_anchors = ecs_matrix.get(c_code, [])
        direct_map: Dict[str, Dict] = {}

        print(f"\n[*] [@Mainland_Probe] 啟動 ITDog 同款七大區 ECS 深度解析 [{c_code.upper()}] (共 {len(c_domains)} 個網域 x {len(c_anchors)} 省錨點)...")

        # A. CNAME 域名解析
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
            extracted = exhaustive_resolve_domain_for_carrier(domain, c_anchors, rounds=1)
            ip_summary = ", ".join([f"{x['ip']}({x['hit_ratio']}%)" for x in extracted[:6]])
            print(f"    -> [{c_code.upper()}] {domain} (prefer: {prefer_reg or 'AUTO'}) 榨出 {len(extracted)} 個唯一 IP: {ip_summary}")

            for item in extracted:
                ip = item["ip"]
                if not is_cf_official_anycast(ip):
                    continue
                domain_map[domain]["ips"].add(ip)

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
                    direct_map[ip]["hit_ratio"] = round(direct_map[ip]["hit_ratio"] + item["hit_ratio"], 2)
                    if lat_val < direct_map[ip]["cn_latency_ms"]:
                        direct_map[ip]["cn_latency_ms"] = lat_val
                    if prefer_reg and not direct_map[ip].get("prefer_region"):
                        direct_map[ip]["prefer_region"] = prefer_reg

        # B. 注入該運營商專屬種子庫 (含實測 sub-100ms 亞太直連 IP/CIDR，解決移動缺乏高質量候選問題)
        seed_path = data_dir / f"seeds_{c_code}.txt"
        seed_ips = _load_seeds_for_carrier(seed_path)
        for s_ip in seed_ips:
            if not is_cf_official_anycast(s_ip):
                continue
            if s_ip not in direct_map:
                direct_map[s_ip] = {
                    "ip": s_ip,
                    "domain": f"seed_{c_code}",
                    "carrier": c_code,
                    "target_carrier": c_code,
                    "cn_latency_ms": 75.0 if c_code == "ct" else (85.0 if c_code == "cm" else 185.0),
                    "speed_mbs": 15.0,
                    "score": 92.0,
                    "hit_ratio": 90.0,
                    "prefer_region": "HK" if c_code == "cm" else ("SG" if c_code == "ct" else "HK"),
                    "is_direct_cname": False
                }

        # C. 注入外部測速源候選
        for ext in external_candidates.get(c_code, []):
            e_ip = ext["ip"]
            if e_ip not in direct_map:
                direct_map[e_ip] = {
                    "ip": e_ip,
                    "domain": ext.get("domain", "ext_feed"),
                    "carrier": c_code,
                    "target_carrier": c_code,
                    "cn_latency_ms": ext.get("cn_latency_ms", 150.0),
                    "speed_mbs": 15.0,
                    "score": ext.get("score", 90.0),
                    "hit_ratio": ext.get("hit_ratio", 90.0),
                    "prefer_region": "",
                    "is_direct_cname": False
                }

        direct_list = sorted(
            direct_map.values(),
            key=lambda x: (x["cn_latency_ms"], -x["hit_ratio"], -x["score"])
        )
        carrier_results[c_code] = direct_list
        print(f"[+] [{c_code.upper()}] 整合 CNAME + 專屬種子 + 外部實測候選，共產出 {len(direct_list)} 顆待驗證 IP")

    return carrier_results, domain_map


def collect_carrier_candidates(
    pool_path: str = "data/cname_pool.json",
    matrix_path: str = "data/cn_ecs_matrix.json"
) -> Dict[str, List[Dict]]:
    results, _ = collect_carrier_candidates_with_domain_map(pool_path, matrix_path)
    return results
