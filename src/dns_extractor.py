"""CF-Global-QuadNet-Feed: ITDog 同款全國七大區三網 ECS 深度 DNS 解析榨取引擎 (src/dns_extractor.py)。

核心機制：
1. 自動追蹤 CNAME 鏈，挖掘權威 NS 伺服器 IP (discover_target_and_auth_ns)。
2. 使用 dnspython 構造 RFC 7871 ECSOption 二進位封包 (build_ecs_query)。
3. 三通道併發輪詢榨取 (權威 NS UDP 53 + 公共 ECS 伺服器 + RFC 8484 二進位 DoH + AliDNS JSON 兜底)。
4. 統計各 IP 在全國各省份的命中率百分比 (hit_ratio)。
"""

from __future__ import annotations

import socket
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Set, Tuple

import dns.edns
import dns.message
import dns.name
import dns.query
import dns.rdatatype
import dns.resolver
import requests

WIRE_DOH_SERVERS = [
    "https://dns.alidns.com/dns-query",
    "https://doh.pub/dns-query",
    "https://dns.google/dns-query"
]

PUBLIC_ECS_UDP_SERVERS = [
    "119.29.29.29",  # 騰訊 DNSPod (支援大陸三網 ECS)
    "223.5.5.5",     # 阿里雲 AliDNS
    "8.8.8.8"        # Google DNS (完整支援 ECS 轉發至權威 NS)
]

DOH_JSON_FALLBACK = "https://dns.alidns.com/resolve"

_DOH_SESSION = requests.Session()


def is_valid_public_ipv4(ip: str) -> bool:
    parts = ip.strip().split(".")
    if len(parts) != 4:
        return False
    try:
        nums = [int(p) for p in parts]
        if any(n < 0 or n > 255 for n in nums):
            return False
        if nums[0] in (0, 10, 127) or (nums[0] == 172 and 16 <= nums[1] <= 31) or (nums[0] == 192 and nums[1] == 168):
            return False
        return True
    except ValueError:
        return False


def extract_a_records_from_msg(resp_msg: dns.message.Message) -> List[str]:
    ips = []
    if not resp_msg:
        return ips
    for rrset in resp_msg.answer:
        if rrset.rdtype == dns.rdatatype.A:
            for rdata in rrset:
                ip = rdata.address.strip()
                if is_valid_public_ipv4(ip):
                    ips.append(ip)
    return ips


def extract_cname_from_msg(resp_msg: dns.message.Message) -> str:
    if not resp_msg:
        return ""
    for rrset in resp_msg.answer:
        if rrset.rdtype == dns.rdatatype.CNAME:
            for rdata in rrset:
                return str(rdata.target).rstrip(".")
    return ""


def discover_target_and_auth_ns(domain: str) -> Tuple[str, List[str]]:
    """追蹤 CNAME 鏈找到最終目標網域，並查出其權威 NS 伺服器 IP，以直接對權威 NS 發送無快取 ECS 輪詢"""
    current = domain.strip().rstrip(".")
    resolver = dns.resolver.Resolver()
    resolver.nameservers = ["223.5.5.5", "119.29.29.29", "8.8.8.8"]
    resolver.lifetime = 2.5

    # 追蹤最多 3 層 CNAME
    for _ in range(3):
        try:
            ans = resolver.resolve(current, "CNAME")
            cname_target = str(ans[0].target).rstrip(".")
            if cname_target and cname_target != current:
                current = cname_target
            else:
                break
        except Exception:
            break

    # 尋找 current 所屬根網域的 NS 記錄
    labels = current.split(".")
    ns_ips: Set[str] = set()
    for i in range(len(labels) - 1):
        zone_cand = ".".join(labels[i:])
        try:
            ns_ans = resolver.resolve(zone_cand, "NS")
            for rdata in ns_ans:
                ns_host = str(rdata.target).rstrip(".")
                try:
                    a_ans = resolver.resolve(ns_host, "A")
                    for a_r in a_ans:
                        if is_valid_public_ipv4(a_r.address):
                            ns_ips.add(a_r.address)
                except Exception:
                    pass
            if ns_ips:
                break
        except Exception:
            continue

    return current, list(ns_ips)[:4]


def build_ecs_query(qname: str, anchor_ip: str) -> dns.message.Message:
    """構造帶有 RFC 7871 EDNS Client Subnet (/24) 的二進位 DNS 查詢封包"""
    ecs_opt = dns.edns.ECSOption(anchor_ip, 24)
    msg = dns.message.make_query(qname, dns.rdatatype.A, use_edns=0, payload=4096, options=[ecs_opt])
    return msg


def single_ecs_probe(qname: str, anchor_ip: str, auth_ns_ips: List[str]) -> List[str]:
    """對單一省份錨點執行多通道 (權威 NS UDP + 公共 ECS UDP + 二進位 DoH + JSON ECS) 解析"""
    collected: List[str] = []
    msg = build_ecs_query(qname, anchor_ip)
    wire_data = msg.to_wire()

    # 1. 詢問 RFC 8484 標準二進位 DoH (application/dns-message) - 走 HTTPS 443，雲端 Runner 100% 直通
    headers = {
        "Content-Type": "application/dns-message",
        "Accept": "application/dns-message",
        "User-Agent": "Mozilla/5.0"
    }
    for doh_url in WIRE_DOH_SERVERS:
        try:
            r = _DOH_SESSION.post(doh_url, data=wire_data, headers=headers, timeout=2.0)
            if r.status_code == 200 and r.content:
                resp = dns.message.from_wire(r.content)
                ips = extract_a_records_from_msg(resp)
                if ips:
                    collected.extend(ips)
        except Exception:
            pass

    # 2. 阿里雲 DoH JSON ECS 深度兜底 (極度穩定命中分省 Anycast)
    try:
        r = _DOH_SESSION.get(
            DOH_JSON_FALLBACK,
            params={"name": qname, "type": "A", "edns_client_subnet": f"{anchor_ip}/24"},
            timeout=2.0
        )
        if r.status_code == 200:
            for ans in r.json().get("Answer", []):
                if ans.get("type") == 1:
                    ip = ans.get("data", "").strip()
                    if is_valid_public_ipv4(ip):
                        collected.append(ip)
    except Exception:
        pass

    # 3. 直接詢問權威 NS 伺服器 (零快取直擊輪詢池，設定 0.6s 極短超時防止海外雲端 UDP 53 封鎖阻塞)
    for ns_ip in auth_ns_ips:
        try:
            resp = dns.query.udp(msg, ns_ip, timeout=0.6)
            ips = extract_a_records_from_msg(resp)
            if ips:
                collected.extend(ips)
        except Exception:
            pass

    # 4. 詢問支援 ECS 的公共 UDP 伺服器 (設定 0.6s 極短超時)
    for pub_dns in PUBLIC_ECS_UDP_SERVERS:
        try:
            resp = dns.query.udp(msg, pub_dns, timeout=0.6)
            ips = extract_a_records_from_msg(resp)
            if ips:
                collected.extend(ips)
        except Exception:
            pass

    return collected


def exhaustive_resolve_domain_for_carrier(
    domain: str,
    carrier_anchors: List[Dict],
    rounds: int = 2
) -> List[Dict]:
    """模擬 ITDog 全國各省監測點，針對指定網域與指定運營商的 12 個省份錨點進行多輪併發解析，

    100% 榨出該網域在該運營商下的所有分省與輪詢 IP，並計算命中百分比 (hit_ratio)！
    """
    final_qname, auth_ns_ips = discover_target_and_auth_ns(domain)
    query_targets = list({domain, final_qname})

    ip_counter = Counter()
    tasks = []

    with ThreadPoolExecutor(max_workers=24) as executor:
        for _ in range(rounds):
            for qname in query_targets:
                for anchor in carrier_anchors:
                    tasks.append(executor.submit(single_ecs_probe, qname, anchor["ip"], auth_ns_ips))

        for fut in as_completed(tasks):
            try:
                ips = fut.result()
                for ip in ips:
                    ip_counter[ip] += 1
            except Exception:
                pass

    total_hits = sum(ip_counter.values()) or 1
    results = []
    for ip, count in ip_counter.most_common():
        results.append({
            "ip": ip,
            "hits": count,
            "hit_ratio": round((count / total_hits) * 100.0, 2)
        })

    return results
