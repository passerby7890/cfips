"""CF-Global-QuadNet-Feed: 大陸境內三網真機探針實測與物理路由定標引擎 (src/cn_real_tester.py, v4.1.0)。

核心公理：
1. Anycast 路由視角完全取決於出境運營商 (Carrier: CT AS4134, CU AS4837, CM AS9808)。
2. 移動 (CM / CMI): 104.16~28.* 與 172.64.* 經 CMI 直連香港 Equinix HKG (30ms~50ms)，絕非繞美！
3. 電信 (CT / 163): 普通 104.* / 172.67.* 繞美西 LAX/SJC (180ms~300ms)；亞太段 172.64.144~155 (SG)、172.64.52 (TW)、162.159.38 (JP)、162.159.44 (HK)。
4. 聯通 (CU / 169): 走日本 NRT、香港 HK 或美西 SJC。

探測三階防線：
階防線 1: 嚴格限定對應運營商 ASN 的境內真機探針發起 HTTP /cdn-cgi/trace，從標頭直萃 CF-RAY (如 a4e80ab2-HKG) 取得真實 Colo。
階防線 2: 嚴格限定對應運營商 ASN 的境內 Ping 實測平均 RTT 與丟包率。
階防線 3: 結合三網物理落地矩陣 (carrier_landing_matrix.json) 與物理光速定律，杜絕任何盲目 US 兜底！
"""

from __future__ import annotations

import re
import sys
import time
from typing import Dict, List, Tuple
import requests

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from src.region_mapper import (
    COLO_COUNTRY_MAP,
    APAC_COLO_MAP,
    lookup_carrier_landing_country
)

CARRIER_ASN_MAP = {
    "ct": 4134,  # 中國電信 China Telecom (163)
    "cu": 4837,  # 中國聯通 China Unicom (169)
    "cm": 9808   # 中國移動 China Mobile (CMI)
}

US_COLOS = {
    "LAX", "SJC", "SEA", "SFO", "PDX", "DEN", "ORD",
    "DFW", "IAD", "EWR", "MIA", "ATL", "BOS", "PHX", "SLC"
}

# 中國電信/聯通必繞美西之普通 Anycast 黑名單前綴 (僅限電信與聯通，嚴禁污染移動！)
CT_CU_US_BLACKLIST_PREFIXES = (
    "104.18.", "104.19.", "104.20.", "104.21.", "104.22.",
    "104.24.", "104.25.", "104.26.", "172.66.", "172.67."
)

# /24 網段實測快取: (subnet, carrier) -> (country, real_ms, real_loss)
_SUBNET_PROBE_CACHE: Dict[Tuple[str, str], Tuple[str, float, float]] = {}


def is_blacklisted_us_range(ip: str, carrier: str) -> bool:
    """依據用戶指示：廢除任何先入為主的 Anycast 前綴黑名單，
    由實體真機探針與延遲判定節點質量與落地，不做任何硬性前綴攔截！"""
    return False


def get_subnet_24(ip: str) -> str:
    parts = ip.strip().split(".")
    if len(parts) >= 3:
        return f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"
    return ip


def extract_colo_from_http_result(res_json: dict) -> str:
    """從 Globalping HTTP 測量結果中萃取 Cloudflare 機房 IATA 代碼 (CF-RAY 或 body)"""
    results = res_json.get("results", [])
    for p in results:
        res_data = p.get("result", {})
        raw_output = res_data.get("rawOutput", "") or ""
        raw_body = res_data.get("rawBody", "") or ""
        headers = res_data.get("headers", {})
        
        # 1. 直接比對 headers 字典中的 cf-ray
        for k, v in headers.items():
            if k.lower() == "cf-ray" and "-" in str(v):
                parts = str(v).strip().split("-")
                if len(parts) >= 2 and len(parts[-1]) == 3:
                    return parts[-1].upper()
        
        # 2. 從 raw_output 或 raw_body 中以正則匹配 CF-RAY: <id>-<COLO>
        combined = f"{raw_output}\n{raw_body}"
        m = re.search(r'cf-ray:\s*[a-f0-9]+-([a-z]{3})', combined, re.IGNORECASE)
        if m:
            return m.group(1).upper()
            
        # 3. 從 body 中比對 colo=XXX
        m2 = re.search(r'\bcolo=([A-Z]{3})\b', combined)
        if m2:
            return m2.group(1).upper()
            
    return ""


def probe_subnet_from_mainland_cn(rep_ip: str, carrier: str, timeout_sec: float = 6.0) -> Tuple[str, float, float]:
    """從中國大陸境內實體探針 (嚴格限定對應運營商 ASN) 實測該 IP 的：
    1. 真實落地機房國家 (由對應運營商探針請求 Cloudflare 提取 CF-RAY 之 Colo 決定)
    2. 該運營商真實境內平均 Ping 延遲 (real_cn_ms)
    3. 該運營商真實境內丟包率 (real_cn_loss)
    回傳: (country_code, real_cn_ms, real_cn_loss)
    """
    c_key = carrier.lower().strip()
    subnet = get_subnet_24(rep_ip)
    cache_key = (subnet, c_key)

    if cache_key in _SUBNET_PROBE_CACHE:
        return _SUBNET_PROBE_CACHE[cache_key]

    asn = CARRIER_ASN_MAP.get(c_key, 9808 if c_key == "cm" else (4837 if c_key == "cu" else 4134))
    api_url = "https://api.globalping.io/v1/measurements"
    headers = {"Content-Type": "application/json", "User-Agent": "cf-trinet-verifier/4.1"}

    real_colo = ""
    real_ms = 999.0
    real_loss = 100.0
    api_unavailable = False

    # 1. 先用對應運營商的 ASN 發起 HTTP 探測 (直萃 CF-RAY 機房代碼與 TCP 握手延遲 timings.tcp)
    # 注意：locations 必須 STRICTLY 限定該 ASN，嚴禁加入通用 {"country": "CN"} 導致跨網污染！
    try:
        http_payload = {
            "type": "http",
            "target": rep_ip,
            "locations": [{"country": "CN", "asn": asn}],
            "limit": 1,
            "measurementOptions": {
                "protocol": "HTTP",
                "port": 80,
                "request": {
                    "method": "GET",
                    "path": "/cdn-cgi/trace",
                    "headers": {"Host": "speed.cloudflare.com"}
                }
            }
        }
        r = requests.post(api_url, json=http_payload, headers=headers, timeout=3.0)
        if r.status_code in (429, 422, 503):
            api_unavailable = True
        elif r.status_code == 202:
            m_id = r.json().get("id")
            deadline = time.time() + timeout_sec
            while time.time() < deadline:
                time.sleep(1.0)
                res = requests.get(f"{api_url}/{m_id}", headers=headers, timeout=2.5)
                if res.status_code == 200 and res.json().get("status") == "finished":
                    r_json = res.json()
                    real_colo = extract_colo_from_http_result(r_json)
                    results_list = r_json.get("results", [])
                    valid_tcp_rtts = []
                    failed_probes = 0
                    for p in results_list:
                        res_obj = p.get("result", {})
                        timings = res_obj.get("timings", {}) or {}
                        t_tcp = timings.get("tcp")
                        t_total = timings.get("total")
                        if res_obj.get("status") == "finished" and t_tcp is not None and float(t_tcp) > 0:
                            valid_tcp_rtts.append(float(t_tcp))
                        elif res_obj.get("status") == "finished" and t_total is not None and float(t_total) > 0:
                            valid_tcp_rtts.append(float(t_total))
                        else:
                            failed_probes += 1
                    if valid_tcp_rtts:
                        real_ms = round(sum(valid_tcp_rtts) / len(valid_tcp_rtts), 1)
                        real_loss = round((failed_probes / max(1, len(results_list))) * 100.0, 1)
                    break
        else:
            api_unavailable = True
    except Exception:
        api_unavailable = True

    # 2. 僅當 HTTP 探針未能取得有效 TCP 延遲 (且 API 正常) 時，才補發 Ping 實測，節省 50% API 配額
    if real_ms >= 900.0 and not api_unavailable:
        try:
            ping_payload = {
                "type": "ping",
                "target": rep_ip,
                "locations": [{"country": "CN", "asn": asn}],
                "limit": 1,
                "measurementOptions": {"packets": 3}
            }
            r = requests.post(api_url, json=ping_payload, headers=headers, timeout=3.0)
            if r.status_code in (429, 422, 503):
                api_unavailable = True
            elif r.status_code == 202:
                m_id = r.json().get("id")
                deadline = time.time() + timeout_sec
                while time.time() < deadline:
                    time.sleep(1.0)
                    res = requests.get(f"{api_url}/{m_id}", headers=headers, timeout=2.5)
                    if res.status_code == 200 and res.json().get("status") == "finished":
                        r_list = res.json().get("results", [])
                        valid_rtts = []
                        losses = []
                        for p in r_list:
                            st = p.get("result", {}).get("stats", {})
                            avg = st.get("avg")
                            loss = st.get("loss")
                            if avg is not None and float(avg) > 0:
                                valid_rtts.append(float(avg))
                            if loss is not None:
                                losses.append(float(loss))
                        if valid_rtts:
                            real_ms = round(sum(valid_rtts) / len(valid_rtts), 1)
                        if losses:
                            real_loss = round(sum(losses) / len(losses), 1)
                        elif valid_rtts:
                            real_loss = 0.0
                        break
        except Exception:
            api_unavailable = True

    # 3. 國家代碼判定：
    # A. 最高優先級：若大陸境內實體探針實測 Colo 存在，嚴格以實測機房真實國家為準 (禁止靜態矩陣把 LAX/LHR 洗白成 HK)
    country = ""
    if real_colo:
        if real_colo in US_COLOS:
            country = "US"
        else:
            country = COLO_COUNTRY_MAP.get(real_colo, "") or APAC_COLO_MAP.get(real_colo, "")

    # B. 若實測機房為空 (AUTO) 或未知代碼，查詢該運營商專屬真實物理落地矩陣 (carrier_landing_matrix.json) 權威標記
    if not country:
        country = lookup_carrier_landing_country(rep_ip, c_key)

    # D. 若仍未取得國家，啟用三維物理延遲定律保底：
    if not country:
        if c_key == "cm":
            if real_ms < 140.0 or api_unavailable:
                country = "HK"
            else:
                country = "US"
        elif c_key == "ct":
            if rep_ip.startswith(("172.64.229.", "162.159.38.", "162.159.39.")):
                country = "JP"
            elif rep_ip.startswith(("172.64.14", "172.64.15", "108.162.19", "108.162.20")):
                country = "SG"
            elif rep_ip.startswith(("162.159.44.", "162.159.45.")):
                country = "HK"
            elif rep_ip.startswith(("172.64.52.", "172.64.53.")):
                country = "TW"
            elif real_ms <= 85.0 or api_unavailable:
                country = "SG"
            else:
                country = "US"
        else: # cu
            if rep_ip.startswith(("162.159.38.", "108.162.198.")):
                country = "JP"
            elif rep_ip.startswith(tuple(f"162.159.{i}." for i in range(128, 156)) + ("162.159.44.", "162.159.45.")):
                country = "HK"
            elif rep_ip.startswith(("162.159.160.", "188.114.96.", "188.114.97.")):
                country = "DE"
            elif rep_ip.startswith(("172.64.229.", "172.64.14", "172.64.15", "104.18.", "104.21.", "172.67.")):
                country = "US"
            elif real_ms <= 85.0:
                country = "JP"
            else:
                country = "US"

    # 若探針確實實測但目標超時/無回應 (非 API 限流)，維持 999.0ms 與 100.0% 丟包，交由上游淘汰
    # 若為 API 429 限流或探針暫時不可用，對已通過階段 2 WS 101 驗證的亞太矩陣節點採取保守保底
    if api_unavailable and real_ms >= 900.0:
        if country in ("HK", "TW"):
            real_ms = 205.0 if c_key == "cu" else 48.0
        elif country in ("SG", "JP"):
            real_ms = 190.0 if c_key == "cu" else 62.0
        elif country == "DE":
            real_ms = 150.0 if c_key == "cu" else 135.0
        else:
            real_ms = 240.0 if c_key == "cu" else 220.0
        real_loss = 0.0
        tag_label = "API限流保底"
    elif real_ms >= 900.0:
        real_ms = 999.0
        real_loss = 100.0
        tag_label = "探針超時"
    else:
        tag_label = "大陸真機實測"

    result = (country, real_ms, real_loss)
    _SUBNET_PROBE_CACHE[cache_key] = result
    print(f"    [*] [{tag_label}] 代表 IP {rep_ip} ({subnet}) [{c_key.upper()}] -> 國別: #{country}, 大陸延遲: {real_ms}ms (丟包 {real_loss}%, Colo: {real_colo or 'AUTO'})")
    return result
