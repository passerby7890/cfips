"""IATA 三字機場代碼 (colo) 與國家/地區映射表。

將 Cloudflare 邊緣機房代碼精準映射至指定地區：
- HK: 香港 (HKG)
- TW: 台灣 (TPE, KHH)
- JP: 日本 (NRT, KIX, FUK, OKA)
- SG: 新加坡 (SIN)
- KR: 韓國 (ICN)
- US: 美國 (SJC, LAX, SEA, SFO, PDX, DEN, ORD, IAD, EWR 等)
- EU: 歐洲 (FRA, AMS, LHR, CDG, ZRH, MAD 等)
"""

from __future__ import annotations

# 地區代碼 -> 機房列表
COUNTRY_TO_COLOS: dict[str, list[str]] = {
    "hk": ["HKG"],
    "tw": ["TPE", "KHH"],
    "jp": ["NRT", "KIX", "FUK", "OKA"],
    "sg": ["SIN"],
    "kr": ["ICN"],
    "us": [
        "SJC", "LAX", "SEA", "SFO", "PDX", "DEN", "ORD", "IAD", "EWR",
        "ATL", "MIA", "DFW", "PHX", "IAH", "BOS", "LAS", "DTW", "MSP",
    ],
    "eu": [
        "FRA", "AMS", "LHR", "CDG", "ZRH", "MAD", "MXP", "VIE", "WAW",
        "ARN", "DUB", "OSL", "CPH", "BRU", "PRG", "BCN", "HEL", "LIS",
    ],
}

# 機房代碼 (大寫) -> 地區代碼 (小寫)
COLO_TO_COUNTRY: dict[str, str] = {}
for country, colos in COUNTRY_TO_COLOS.items():
    for c in colos:
        COLO_TO_COUNTRY[c.upper()] = country

COUNTRY_NAMES: dict[str, str] = {
    "hk": "香港",
    "tw": "台灣",
    "jp": "日本",
    "sg": "新加坡",
    "kr": "韓國",
    "us": "美國",
    "eu": "歐洲",
}


def colo_to_country(colo: str | None) -> str | None:
    """將 Cloudflare IATA 機房代碼映射為支援的地區代碼（若不在清單內回傳 None）。"""
    if not colo:
        return None
    return COLO_TO_COUNTRY.get(str(colo).strip().upper())


def get_country_name(country: str) -> str:
    """取得地區的中文名稱。"""
    return COUNTRY_NAMES.get(country.lower(), country.upper())


def get_supported_countries() -> list[str]:
    """取得所有支援的目標國家/地區清單。"""
    return list(COUNTRY_TO_COLOS.keys())
