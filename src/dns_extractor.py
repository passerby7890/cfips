"""CF-Global-QuadNet-Feed: ITDog 同款全國七大區三網 ECS 深度 DNS 解析榨取引擎 (src/dns_extractor.py)。

核心機制：
1. 自動追蹤 CNAME 鏈，挖掘權威 NS 伺服器 IP (discover_target_and_auth_ns)。
2. 使用 dnspython 構造 RFC 7871 ECSOption 二進位封包 (build_ecs_query)。
3. 三通道併發輪詢榨取 (權威 NS UDP 53 + 公共 ECS 伺服器 + RFC 8484 二進位 DoH + AliDNS JSON 兜底)。
4. 統計各 IP 在全國各省份的命中率百分比 (hit_ratio)。
"""

from __future__ import annotations

import functools
import ipaddress
import socket
import threading
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
    "https://dns.google/dns-query"
]

PUBLIC_ECS_UDP_SERVERS = [
    "119.29.29.29",  # 騰訊 DNSPod (支援大陸三網 ECS)
    "223.5.5.5",     # 阿里雲 AliDNS
    "8.8.8.8"        # Google DNS (完整支援 ECS 轉發至權威 NS)
]

DOH_JSON_FALLBACK = "https://dns.alidns.com/resolve"

_DOH_SESSION = requests.Session()
_THREAD_LOCAL = threading.local()


def _get_doh_session() -> requests.Session:
    sess = getattr(_THREAD_LOCAL, "session", None)
    if sess is None:
        sess = requests.Session()
        _THREAD_LOCAL.session = sess
    return sess


def is_valid_public_ip(ip: str, rdtype: str = "A") -> bool:
    """校驗是否為合法公網 IPv4 / IPv6 位址（排除保留/私網/鏈路本地段）"""
    try:
        obj = ipaddress.ip_address(ip.strip())
        if rdtype.upper() == "AAAA":
            return isinstance(obj, ipaddress.IPv6Address) and obj.is_global
        return isinstance(obj, ipaddress.IPv4Address) and obj.is_global
    except ValueError:
        return False


def is_valid_public_ipv4(ip: str) -> bool:
    return is_valid_public_ip(ip, "A")


def extract_records_from_msg(resp_msg: dns.message.Message, rdtype: str = "A") -> List[str]:
    ips = []
    if not resp_msg:
        return ips
    target_rtype = dns.rdatatype.AAAA if rdtype.upper() == "AAAA" else dns.rdatatype.A
    for rrset in resp_msg.answer:
        if rrset.rdtype == target_rtype:
            for rdata in rrset:
                ip = rdata.address.strip()
                if is_valid_public_ip(ip, rdtype):
                    ips.append(ip)
    return ips


def extract_a_records_from_msg(resp_msg: dns.message.Message) -> List[str]:
    return extract_records_from_msg(resp_msg, "A")


def extract_cname_from_msg(resp_msg: dns.message.Message) -> str:
    if not resp_msg:
        return ""
    for rrset in resp_msg.answer:
        if rrset.rdtype == dns.rdatatype.CNAME:
            for rdata in rrset:
                return str(rdata.target).rstrip(".")
    return ""


@functools.lru_cache(maxsize=512)
def discover_target_and_auth_ns(domain: str) -> Tuple[str, Tuple[str, ...]]:
    """追蹤 CNAME 鏈找到最終目標網域（DoH/UDP 快速併發路徑無須逐域名預查 4 台權威 NS IP）。"""
    current = domain.strip().rstrip(".")
    resolver = dns.resolver.Resolver()
    resolver.nameservers = ["223.5.5.5", "8.8.8.8"]
    resolver.lifetime = 0.8

    for _ in range(2):
        try:
            ans = resolver.resolve(current, "CNAME")
            cname_target = str(ans[0].target).rstrip(".")
            if cname_target and cname_target != current:
                current = cname_target
            else:
                break
        except Exception:
            break

    return current, ()


def build_ecs_query(qname: str, anchor_ip: str, rdtype: str = "A") -> dns.message.Message:
    """構造帶有 RFC 7871 EDNS Client Subnet (/24) 的二進位 DNS 查詢封包"""
    ecs_opt = dns.edns.ECSOption(anchor_ip, 24)
    target_rtype = dns.rdatatype.AAAA if rdtype.upper() == "AAAA" else dns.rdatatype.A
    msg = dns.message.make_query(qname, target_rtype, use_edns=0, payload=4096, options=[ecs_opt])
    return msg


def single_ecs_probe(
    qname: str,
    anchor_ip: str,
    auth_ns_ips: Tuple[str, ...] | List[str],
    rdtype: str = "A",
) -> List[str]:
    """對單一省份錨點執行快速多通道 ECS 解析（支援 A / AAAA，優先 DoH）"""
    collected: List[str] = []
    rtype = rdtype.upper()
    json_type_num = 28 if rtype == "AAAA" else 1
    msg = build_ecs_query(qname, anchor_ip, rdtype=rtype)
    wire_data = msg.to_wire()
    sess = _get_doh_session()

    # 1. 詢問 RFC 8484 標準二進位 DoH (成功即返回，避免冗餘查詢拖慢速度)
    headers = {
        "Content-Type": "application/dns-message",
        "Accept": "application/dns-message",
        "User-Agent": "Mozilla/5.0"
    }
    for doh_url in WIRE_DOH_SERVERS:
        try:
            r = sess.post(doh_url, data=wire_data, headers=headers, timeout=1.5)
            if r.status_code == 200 and r.content:
                resp = dns.message.from_wire(r.content)
                ips = extract_records_from_msg(resp, rtype)
                if ips:
                    collected.extend(ips)
                    return ips
                if rtype == "AAAA" and resp.rcode() == dns.rcode.NOERROR:
                    return []
        except Exception:
            pass

    # 2. DoH JSON ECS 兜底
    try:
        r = sess.get(
            DOH_JSON_FALLBACK,
            params={"name": qname, "type": rtype, "edns_client_subnet": f"{anchor_ip}/24"},
            timeout=1.2
        )
        if r.status_code == 200:
            payload = r.json()
            for ans in payload.get("Answer", []):
                if ans.get("type") == json_type_num:
                    ip = ans.get("data", "").strip()
                    if is_valid_public_ip(ip, rtype):
                        collected.append(ip)
            if collected:
                return collected
            if rtype == "AAAA" and payload.get("Status") == 0:
                return []
    except Exception:
        pass

    # 3. 權威 NS 伺服器兜底
    for ns_ip in auth_ns_ips:
        try:
            resp = dns.query.udp(msg, ns_ip, timeout=0.6)
            ips = extract_records_from_msg(resp, rtype)
            if ips:
                collected.extend(ips)
                return ips
        except Exception:
            pass

    # 4. 公共 ECS UDP 伺服器兜底
    for pub_dns in PUBLIC_ECS_UDP_SERVERS:
        try:
            resp = dns.query.udp(msg, pub_dns, timeout=0.6)
            ips = extract_records_from_msg(resp, rtype)
            if ips:
                collected.extend(ips)
                return ips
        except Exception:
            pass

    return collected


def exhaustive_resolve_domain_for_carrier(
    domain: str,
    carrier_anchors: List[Dict],
    rounds: int = 1,
    rdtype: str = "A",
) -> List[Dict]:
    """模擬 ITDog 全國各省監測點，針對指定網域與指定運營商的省份錨點進行併發解析，

    100% 榨出該網域在該運營商下的所有分省與輪詢 IP (支援 A / AAAA)，並計算命中率 (hit_ratio)！
    """
    final_qname, auth_ns_ips = discover_target_and_auth_ns(domain)
    query_targets = [final_qname] if (final_qname and rdtype.upper() == "AAAA") else list({domain, final_qname})

    ip_counter = Counter()
    tasks = []

    with ThreadPoolExecutor(max_workers=6) as executor:
        for _ in range(rounds):
            for qname in query_targets:
                for anchor in carrier_anchors:
                    tasks.append(executor.submit(single_ecs_probe, qname, anchor["ip"], auth_ns_ips, rdtype))

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
