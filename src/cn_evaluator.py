"""大陸三網 BGP 路由基因與境內指標評估器 (v2.1.0 專家終極版)。

站在「大陸三網真實境內視角」為 2,000+ 候選 IP 池進行運營商專屬評分與境內延遲估算：
- 核心鐵律：中國電信 (CT) 嚴格過濾 104.16.*, 104.17.*, 104.18.*, 172.64.* 等繞美 160ms+ 假香港/台灣網段
- 第一引擎：大陸實體探針即時數據 (WeTest, HostMonit 等)
- 第二引擎：電信 / 聯通 / 移動專屬 BGP 亞太直連神仙網段矩陣 (ITDog 實測 30ms ~ 75ms)
- 硬性門檻：大陸境內延遲 >= 100ms 一律封頂 10 分，嚴防進入三網前 50 名
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
        # 電信特批直連香港/新加坡/日本/台灣核心段 (實測 33~65ms，嚴格排除 104.16/104.17/141.101 等繞美段)
        "108.162.192.0/18",  # 涵蓋 108.162.198 (HK/JP), 108.162.236 (HK), 108.162.240 (SG)
        "162.159.192.0/21",  # 涵蓋 162.159.192 (HK), 162.159.193 (JP), 162.159.195 (HK)
        "162.159.128.0/18",  # 涵蓋 162.159.130 (JP), 162.159.140 (SG), 162.159.152 (TW)
        "162.159.224.0/19",  # 涵蓋 162.159.236 (TW), 162.159.237 (HK)
        "198.41.208.0/20",   # 涵蓋 198.41.208 (HK), 198.41.209 (HK)
        "103.21.244.0/22",   # 歷史亞太 APNIC 核心段 (HKIX 直連)
        "103.22.200.0/22",
        "103.31.4.0/22",
    ],
    "cu": [
        # 聯通直連日本/香港/新加坡核心段
        "108.162.192.0/18",
        "162.159.128.0/18",
        "162.159.192.0/21",
        "162.159.224.0/19",
        "141.101.112.0/20",
        "104.16.50.0/24",    # 聯通 AS4837 東京 NRT 專線
        "104.16.141.0/24",   # 聯通 AS4837 日本
        "103.21.244.0/22",
        "103.22.200.0/22",
        "103.31.4.0/22",
        "198.41.208.0/20",
    ],
    "cm": [
        # 移動 CMI 核心直連香港/新加坡/台灣 (實測 25~55ms)
        "172.64.0.0/13",     # 移動 CMI 香港直連核心段 (172.64.32, 172.64.96, 172.64.229)
        "104.28.0.0/14",     # 移動 CMI 香港核心段 (104.28.16, 104.28.17)
        "108.162.192.0/18",
        "162.158.0.0/15",
        "198.41.128.0/17",
        "141.101.112.0/20",
        "103.21.244.0/22",
        "103.22.200.0/22",
    ]
}

# 運營商強制繞美高延遲黑名單段 (在該運營商視角下 100% 走太平洋海底光纜至美西 150ms~190ms)
CARRIER_DETOUR_CIDRS: Dict[str, List[str]] = {
    "ct": [
        # 電信 163 骨幹網對普通 Anycast 100% 繞美西 (聖荷西/洛杉磯，實測 167ms)
        "104.16.0.0/12",     # 涵蓋 104.16.* ~ 104.31.* 全段
        "141.101.0.0/16",    # 141.101 全段在電信走美西 (實測 167ms)
        "172.64.0.0/13",     # 涵蓋 172.64.* ~ 172.71.* (電信無 CMI，走美西)
        "173.245.48.0/20",
        "198.41.128.0/19",
    ],
    "cu": [
        # 聯通繞美段 (排除 104.16.50 與 104.16.141 日本直連)
        "104.17.0.0/16",
        "104.18.0.0/15",
        "104.20.0.0/14",
        "104.24.0.0/13",
        "172.64.0.0/13",
        "173.245.48.0/20",
    ],
    "cm": [
        # 移動普通繞美段
        "104.16.0.0/13",
        "104.18.0.0/15",
        "104.20.0.0/14",
        "172.66.0.0/15",
        "173.245.48.0/20",
    ]
}

_TIER_S_NETS: Dict[str, List[ipaddress.IPv4Network]] = {
    k: [ipaddress.IPv4Network(c) for c in v]
    for k, v in CARRIER_APAC_TIER_S_CIDRS.items()
}
_DETOUR_NETS: Dict[str, List[ipaddress.IPv4Network]] = {
    k: [ipaddress.IPv4Network(c) for c in v]
    for k, v in CARRIER_DETOUR_CIDRS.items()
}

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

    # 1. 境內實體探針數據採計 (WeTest, HostMonit 等)
    cn_lat = item.get(f"cn_{c}_latency_ms") or (
        item.get("cn_latency_ms") if source_carrier in (c, "all", "") else None
    )
    source_hint = item.get("source_hint") or {}
    if not cn_lat and source_hint.get("latency_ms"):
        h_carrier = str(source_hint.get("carrier") or "").lower()
        if h_carrier in (c, "all", ""):
            cn_lat = source_hint.get("latency_ms")

    cn_spd = item.get(f"cn_{c}_speed_mbps") or item.get("cn_speed_mbps") or source_hint.get("download_speed", 0.0)
    cn_loss = float(item.get("cn_loss_percent") or source_hint.get("loss_rate") or item.get("loss_percent") or 0.0)

    is_tier_s = match_cidr_list(ip, _TIER_S_NETS.get(c, []))
    is_detour = match_cidr_list(ip, _DETOUR_NETS.get(c, []))
    is_unicast_apac = (not is_cf_official_anycast(ip)) and (country in APAC_COUNTRIES)

    # 2. 決定該運營商視角下的預估境內延遲 (est_cn_latency_ms)
    # 電信 (CT) 核心防禦：若命中 104.16.*, 104.17.*, 104.18.*, 172.64.* 等繞美段，且無該網 < 90ms 境內實測，一律判定為高延遲繞美！
    if is_detour and (not cn_lat or float(cn_lat) >= 90.0):
        est_latency = 165.0
    elif cn_lat and float(cn_lat) > 0:
        est_latency = float(cn_lat)
    elif is_unicast_apac:
        # 第三方亞太反代 Unicast IP (HK/TW/JP/SG/KR)
        est_latency = 42.0 if country in ("HK", "TW") else 58.0
    elif is_tier_s:
        # 命中該運營商的 BGP 亞太直連神仙段 (如 108.162.198.x, 141.101.114.x)
        est_latency = 45.0 if c in ("ct", "cm") else 55.0
    elif country in APAC_COUNTRIES:
        # 種子庫或情報源標記為亞太，但非特批 Tier-S 段
        if c == "ct":
            # 對於電信，非特批段走 Anycast 必然繞美
            est_latency = 160.0
        elif c == "cm":
            est_latency = 65.0
        else:
            est_latency = 85.0
    elif is_detour:
        est_latency = 170.0
    else:
        # 一般美西 Anycast 段
        est_latency = 150.0

    # 3. 計算境內視角綜合分數 (0 ~ 100 分，徹底脫鉤美國 Runner 延遲)
    # (A) 境內延遲得分 (滿分 45 分：<= 95ms 為亞太低延遲區間，> 95ms 降至 0 分，徹底淘汰繞美段)
    if est_latency <= 95.0:
        # 35ms 拿 45 分，95ms 拿 30 分
        s_latency = 30.0 + ((95.0 - est_latency) / (95.0 - 35.0)) * 15.0
    else:
        # 超過 95ms 嚴格給予 0 分延遲獎勵
        s_latency = 0.0

    # (B) BGP 路由階級與地區加權 (滿分 30 分)
    s_bgp = 0.0
    if (source_carrier == c or source_hint.get("carrier") == c) and cn_lat and est_latency <= 95.0:
        s_bgp += 18.0  # 境內該網低延遲實測認證
    elif (source_carrier == c or source_hint.get("carrier") == c) and cn_lat:
        s_bgp += 2.0

    if is_tier_s or is_unicast_apac:
        s_bgp += 20.0  # 亞太直連神仙段保送加分
    elif country in APAC_COUNTRIES and est_latency <= 95.0:
        s_bgp += 10.0

    if is_detour:
        s_bgp -= 50.0  # 嚴懲繞美段

    # (C) 境內頻寬與回源吞吐得分 (滿分 15 分)
    effective_spd = float(cn_spd) if (cn_spd and float(cn_spd) > 0) else float(item.get("speed_mbps") or item.get("download_speed") or 50.0)
    s_speed = min(15.0, (min(effective_spd, 150.0) / 150.0) * 15.0)

    # (D) 穩定度與冷門鄰居獎勵 (滿分 10 分)
    s_clean = 3.0 if item.get("is_shifted_neighbor", False) else 1.0
    s_loss_bonus = max(0.0, 7.0 - (cn_loss * 2.0))

    total_score = round(s_latency + s_bgp + s_speed + s_clean + s_loss_bonus, 2)
    total_score = max(0.0, total_score)

    # 門檻鐵律：若預估延遲 >= 100ms 或命中繞美段，總分最高封頂 10 分，確保絕對無法進入前 50 名！
    if est_latency >= 100.0 or is_detour:
        total_score = min(total_score, 10.0)

    evaluated = dict(item)
    evaluated["est_cn_latency_ms"] = round(est_latency, 1)
    evaluated["cn_latency_ms"] = round(est_latency, 1)
    evaluated["final_score"] = total_score
    evaluated["target_carrier"] = c

    # 標籤精確校準：若是繞美高延遲節點，絕不准標記為 HK/TW/JP/SG
    curr_country = (evaluated.get("country") or evaluated.get("source_country") or evaluated.get("country_code") or "").upper()
    if est_latency >= 100.0 and curr_country in APAC_COUNTRIES:
        evaluated["country"] = "US"
        evaluated["country_code"] = "US"
    elif is_tier_s and (curr_country in ("US", "CF", "") or not curr_country):
        evaluated["country"] = "SG" if "108.162.192" in ip or "108.162.240" in ip else ("TW" if "162.159.236" in ip or "141.101.113" in ip else "HK")
        evaluated["country_code"] = evaluated["country"]

    return evaluated
