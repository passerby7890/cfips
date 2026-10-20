"""IATA 機房代碼 (colo) 與國家標籤多層級精確解析器 (v1.1.0)。

修復 GitHub Actions 海外 Runner 覆蓋 Anycast 國家標籤問題：
- is_cf_official_anycast(ip_str): 判斷是否為 Cloudflare 官方 Anycast 網段
- get_country_by_colo(colo): 依 Colo 機房回傳大寫國家簡寫代碼 (如 TPE -> TW, HKG -> HK)
- resolve_country_tag(item, runner_trace_colo): 依 4 層優先級解析最終國家標籤，嚴禁海外 Runner 覆蓋
"""

from __future__ import annotations

import ipaddress
from typing import Any, Dict, Optional

# Cloudflare 官方 IPv4 Anycast 核心網段
CF_OFFICIAL_CIDRS = [
    "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "104.16.0.0/13", "104.24.0.0/14", "108.162.192.0/18",
    "131.0.72.0/22", "141.101.64.0/18", "162.158.0.0/15",
    "172.64.0.0/13", "173.245.48.0/20", "188.114.96.0/20",
    "190.93.240.0/20", "197.234.240.0/22", "198.41.128.0/17"
]
_CF_NETWORKS = [ipaddress.IPv4Network(cidr) for cidr in CF_OFFICIAL_CIDRS]

COLO_COUNTRY_MAP: Dict[str, str] = {
    # 亞太區
    "HKG": "HK", "MFM": "MO",
    "TPE": "TW", "KHH": "TW",
    "NRT": "JP", "KIX": "JP", "FUK": "JP", "OKA": "JP", "NGO": "JP",
    "SIN": "SG",
    "ICN": "KR", "PUS": "KR",
    "BKK": "TH", "KUL": "MY", "MNL": "PH", "SGN": "VN", "HAN": "VN", "CGK": "ID",
    "SYD": "AU", "MEL": "AU", "AKL": "NZ",
    # 美洲區
    "SJC": "US", "LAX": "US", "SEA": "US", "SFO": "US", "PDX": "US",
    "DEN": "US", "ORD": "US", "DFW": "US", "IAD": "US", "EWR": "US",
    "MIA": "US", "ATL": "US", "BOS": "US", "PHX": "US", "SLC": "US",
    "YVR": "CA", "YYZ": "CA",
    # 歐洲區
    "FRA": "DE", "MUC": "DE", "AMS": "NL", "LHR": "GB", "MAN": "GB",
    "CDG": "FR", "MRS": "FR", "MAD": "ES", "ZRH": "CH", "MIL": "IT",
    "WAW": "PL", "ARN": "SE", "HEL": "FI", "VIE": "AT"
}

COUNTRY_NAMES: Dict[str, str] = {
    "hk": "香港", "tw": "台灣", "jp": "日本", "sg": "新加坡", "kr": "韓國",
    "us": "美國", "de": "德國", "gb": "英國", "fr": "法國", "ca": "加拿大",
    "cf": "Cloudflare",
}


def is_cf_official_anycast(ip_str: str) -> bool:
    """判斷是否屬於 Cloudflare 官方 Anycast 網段"""
    try:
        ip_obj = ipaddress.IPv4Address(ip_str.strip())
        return any(ip_obj in net for net in _CF_NETWORKS)
    except Exception:
        return True


def get_country_by_colo(colo: str | None) -> str:
    """由 Colo 代碼解析大寫國家代碼 (如 TPE -> TW, HKG -> HK)"""
    if not colo:
        return ""
    return COLO_COUNTRY_MAP.get(str(colo).strip().upper(), "")


def colo_to_country(colo: str | None) -> str | None:
    """相容舊版接口 (回傳小寫或 None)"""
    if not colo:
        return None
    c = get_country_by_colo(colo)
    return c.lower() if c else None


def get_country_code(colo: str | None, loc: str | None = None) -> str:
    """取得國家大寫二字碼"""
    if colo:
        c = get_country_by_colo(colo)
        if c:
            return c
    if loc and len(str(loc).strip()) == 2:
        return str(loc).strip().upper()
    return "US"


def get_country_name(country: str) -> str:
    """取得地區的中文名稱"""
    return COUNTRY_NAMES.get(str(country).lower(), str(country).upper())


def get_supported_countries() -> list[str]:
    """取得主要支援的國家/地區清單"""
    return ["hk", "tw", "jp", "sg", "kr", "us", "de"]


def resolve_country_tag(item: Dict[str, Any], runner_trace_colo: str = "") -> str:
    """
    多層級決定最終輸出的 #COUNTRY 標籤，防止 GitHub 美國 Runner 洗掉亞太標籤：
    優先級 1: 情報源 / 種子庫在大陸境內實測的 source_colo (如 HKG, TPE, NRT, SIN)
    優先級 2: 情報源 / 種子庫明確標註的 source_country (如 #TW, #HK, #JP, #SG)
    優先級 3: 原生 country / countries 標註
    優先級 4: Runner 測得的 trace_colo (若在台灣則為 TW，日本則為 JP)
    優先級 5: 最後兜底 (CF)
    """
    ip = str(item.get("ip", "")).strip()
    source_colo = str(item.get("source_colo") or "").strip().upper()
    source_country = str(item.get("source_country") or "").strip().upper()

    # 1. 優先採計境內探針實測 Colo
    if source_colo:
        mapped = get_country_by_colo(source_colo)
        if mapped:
            return mapped

    # 2. 優先採計情報源/種子庫原生國家標籤
    if source_country and source_country not in ("", "UNKNOWN", "CF", "ANYCAST", "NONE", "NULL"):
        return source_country

    # 3. 檢查 item["country"] 或 item["countries"] 原生標註
    orig_country = str(item.get("country") or "").strip().upper()
    if orig_country and orig_country not in ("", "UNKNOWN", "CF", "ANYCAST", "NONE", "NULL"):
        return orig_country

    for c in item.get("countries", []):
        c_up = str(c or "").strip().upper()
        if c_up and c_up not in ("", "UNKNOWN", "CF", "ANYCAST", "NONE", "NULL"):
            return c_up

    # 4. Runner 實測 Colo 解析
    runner_country = get_country_by_colo(runner_trace_colo)
    if runner_country:
        return runner_country

    # 5. 最後兜底
    return "CF"

