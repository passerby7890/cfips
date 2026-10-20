"""大陸三網 BGP 路由基因與境內指標評估器 (v1.4.0)。

負責站在「大陸三網境內視角」為 2,000+ 候選 IP 池進行運營商專屬評分與境內延遲估算：
- 第一引擎：大陸實體探針即時數據 (WeTest, HostMonit 等) 直連
- 第二引擎：電信 / 聯通 / 移動專屬 BGP 亞太直連神仙網段矩陣
- 徹底脫鉤 GitHub Actions 美國測試機延遲，防止亞太直連節點被海外測試機反向淘汰
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Any, Dict, List

from .region_mapper import is_cf_official_anycast

log = logging.getLogger(__name__)

# =====================================================================
# 大陸三網 BGP 亞太直連神仙網段基因庫 (ITDog 實測 30ms ~ 75ms 核心段)
# =====================================================================
CARRIER_APAC_TIER_S_CIDRS: Dict[str, List[str]] = {
    "ct": [
        # 電信直連香港/新加坡/日本特批段 (如 108.162.198.8 實測 33~55ms)
        "108.162.192.0/18",
        "162.159.192.0/21",
        "141.101.114.0/23",
        "198.41.208.0/20",
        "172.64.32.0/20",
        "103.21.244.0/22",
        "103.22.200.0/22",
    ],
    "cu": [
        # 聯通直連日本/新加坡/香港/美西低延遲段
        "108.162.192.0/18",
        "162.159.128.0/18",
        "141.101.112.0/20",
        "104.16.50.0/24",
        "104.24.0.0/14",
        "103.21.244.0/22",
    ],
    "cm": [
        # 移動 CMI 直連香港/新加坡/亞太段 (實測 25~60ms)
        "172.64.0.0/13",
        "104.28.0.0/14",
        "108.162.192.0/18",
        "162.158.0.0/15",
        "198.41.128.0/17",
        "103.21.244.0/22",
    ]
}

# 容易在大陸三網繞美東/歐洲或高丟包的普通大眾網段 (給予降權懲罰)
PENALTY_HIGH_LATENCY_CIDRS: List[str] = [
    "104.18.0.0/15",
    "104.20.0.0/15",
    "104.21.0.0/16",
    "104.22.0.0/16",
    "172.66.0.0/16",
    "172.67.0.0/16",
]

_TIER_S_NETS: Dict[str, List[ipaddress.IPv4Network]] = {
    k: [ipaddress.IPv4Network(c) for c in v]
    for k, v in CARRIER_APAC_TIER_S_CIDRS.items()
}
_PENALTY_NETS: List[ipaddress.IPv4Network] = [
    ipaddress.IPv4Network(c) for c in PENALTY_HIGH_LATENCY_CIDRS
]

APAC_COUNTRIES = {"HK", "TW", "SG", "JP", "KR", "MO", "MY", "TH", "VN", "PH"}


def match_cidr_list(ip_str: str, net_list: List[ipaddress.IPv4Network]) -> bool:
    try:
        ip_obj = ipaddress.IPv4Address(ip_str.strip())
        return any(ip_obj in net for net in net_list)
    except Exception:
        return False


def evaluate_for_carrier(item: Dict[str, Any], carrier: str) -> Dict[str, Any]:
    """針對特定大陸運營商 (ct / cu / cm) 進行境內視角評估：

    計算預估境內延遲 (est_cn_latency_ms) 與三網適配優先分 (final_score)
    """
    c = carrier.lower()
    ip = str(item.get("ip", "")).strip()
    country = str(item.get("country") or item.get("source_country") or item.get("country_code") or "").upper()
    source_carrier = str(item.get("source_carrier") or "").lower()

    # 1. 若該 IP 帶有境內實體探針實測的延遲與帶寬 (且運營商匹配或通用)
    cn_lat = item.get(f"cn_{c}_latency_ms") or (
        item.get("cn_latency_ms") if source_carrier in (c, "all", "") else None
    )
    # 若在 source_hint 中帶有境內探針數據
    source_hint = item.get("source_hint") or {}
    if not cn_lat and source_hint.get("latency_ms"):
        h_carrier = str(source_hint.get("carrier") or "").lower()
        if h_carrier in (c, "all", ""):
            cn_lat = source_hint.get("latency_ms")

    cn_spd = item.get(f"cn_{c}_speed_mbps") or item.get("cn_speed_mbps") or source_hint.get("download_speed", 0.0)
    cn_loss = float(item.get("cn_loss_percent") or source_hint.get("loss_rate") or item.get("loss_percent") or 0.0)

    is_tier_s = match_cidr_list(ip, _TIER_S_NETS.get(c, []))
    is_penalty = match_cidr_list(ip, _PENALTY_NETS)
    is_unicast_apac = (not is_cf_official_anycast(ip)) and (country in APAC_COUNTRIES)

    # 2. 決定該運營商視角下的預估境內延遲 (est_cn_latency_ms)
    if cn_lat and float(cn_lat) > 0:
        est_latency = float(cn_lat)
    elif is_unicast_apac:
        # 第三方亞太反代 Unicast IP (HK/TW/JP/SG/KR)
        est_latency = 42.0 if country in ("HK", "TW") else 58.0
    elif is_tier_s:
        # 命中該運營商的 BGP 亞太直連神仙段 (如 108.162.198.x)
        est_latency = 45.0 if c in ("ct", "cm") else 55.0
    elif country in APAC_COUNTRIES:
        # 種子庫標記為亞太區但非特批段
        est_latency = 65.0 if c == "cm" else 95.0
    elif is_penalty:
        # 命中 104.18.x.x 等繞美高延遲段
        est_latency = 185.0
    else:
        # 一般美西 Anycast 段
        est_latency = 150.0

    # 3. 計算境內視角綜合分數 (0 ~ 100 分，徹底脫鉤美國 Runner 延遲)
    # (A) 境內延遲得分 (滿分 45 分：<= 95ms 為亞太低延遲區間，> 95ms 大幅降權防止進入前 10 名)
    if est_latency <= 95.0:
        # 35ms 拿 45 分，95ms 拿 30 分
        s_latency = 30.0 + ((95.0 - est_latency) / (95.0 - 35.0)) * 15.0
    else:
        # 95ms 以上大幅降權 (最高 10 分，200ms 為 0 分)，確保絕對進不了前 10 名
        s_latency = max(0.0, ((200.0 - est_latency) / (200.0 - 95.0)) * 10.0)

    # (B) BGP 路由階級與地區加權 (滿分 30 分)
    s_bgp = 0.0
    if (source_carrier == c or source_hint.get("carrier") == c) and cn_lat and est_latency <= 95.0:
        s_bgp += 18.0  # 境內該網低延遲實測認證
    elif (source_carrier == c or source_hint.get("carrier") == c) and cn_lat:
        s_bgp += 5.0   # 雖然有實測但延遲高於 95ms，僅作備援
    if is_tier_s or is_unicast_apac:
        s_bgp += 15.0  # 亞太直連神仙段保送加分
    elif country in APAC_COUNTRIES:
        s_bgp += 10.0
    if is_penalty:
        s_bgp -= 25.0  # 嚴懲普通繞美高延遲段
    s_bgp = max(0.0, min(30.0, s_bgp))

    # (C) 境內頻寬與回源吞吐得分 (滿分 15 分)
    effective_spd = float(cn_spd) if (cn_spd and float(cn_spd) > 0) else float(item.get("speed_mbps") or item.get("download_speed") or 50.0)
    s_speed = min(15.0, (min(effective_spd, 150.0) / 150.0) * 15.0)

    # (D) 穩定度與冷門鄰居獎勵 (滿分 10 分)
    s_clean = 3.0 if item.get("is_shifted_neighbor", False) else 1.0
    s_loss_bonus = max(0.0, 7.0 - (cn_loss * 2.0))

    total_score = round(s_latency + s_bgp + s_speed + s_clean + s_loss_bonus, 2)

    evaluated = dict(item)
    evaluated["est_cn_latency_ms"] = round(est_latency, 1)
    evaluated["cn_latency_ms"] = round(est_latency, 1)
    evaluated["final_score"] = total_score
    evaluated["target_carrier"] = c

    # 若為亞太直連神仙段且尚未標記國家，預設標記為 SG/HK 亞太標籤而非 US
    curr_country = (evaluated.get("country") or evaluated.get("country_code") or "").upper()
    if is_tier_s and curr_country in ("US", "CF", ""):
        evaluated["country"] = "SG" if "108.162." in ip else "HK"
        evaluated["country_code"] = evaluated["country"]

    return evaluated
