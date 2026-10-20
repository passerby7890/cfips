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

# 全局母體網段 -> 國家對照快取 (由種子庫與已知亞太段預載)
_SUBNET_COUNTRY_MAP: list[tuple[ipaddress.IPv4Network, str]] = []


def init_subnet_country_map(data_dir: Any = None) -> list[tuple[ipaddress.IPv4Network, str]]:
    """掃描種子庫與分國庫中的所有 CIDR，建構網段到國家的精確映射表"""
    global _SUBNET_COUNTRY_MAP
    from pathlib import Path
    if data_dir is None:
        data_dir = Path(__file__).resolve().parent.parent / "data"
    else:
        data_dir = Path(data_dir)

    res = []
    if data_dir.is_dir():
        for p in data_dir.glob("**/*.txt"):
            try:
                for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    tag = ""
                    if "#" in line:
                        parts = line.split("#", 1)
                        line, tag = parts[0].strip(), parts[1].strip().upper()
                    if not tag and "countries" in str(p):
                        tag = p.stem.upper()
                    if "/" in line:
                        try:
                            net = ipaddress.ip_network(line, strict=False)
                            if tag and tag not in ("", "UNKNOWN", "CF", "ANYCAST"):
                                res.append((net, tag))
                        except Exception:
                            pass
            except Exception:
                pass
    res.sort(key=lambda x: x[0].prefixlen, reverse=True)
    _SUBNET_COUNTRY_MAP = res
    return res


def lookup_subnet_country(ip_str: str) -> str:
    """由 IP 查詢所屬的已知種子網段國家代碼"""
    if not ip_str:
        return ""
    if not _SUBNET_COUNTRY_MAP:
        init_subnet_country_map()
    try:
        ip_obj = ipaddress.IPv4Address(ip_str.strip())
        for net, tag in _SUBNET_COUNTRY_MAP:
            if ip_obj in net:
                return tag
    except Exception:
        pass
    return ""


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


def normalize_country_code(raw: str | None) -> str:
    """將任意格式的地區標籤正規化為 ISO 雙字元大寫代碼 (例如 'jp 【日本】 JP' -> 'JP')"""
    if not raw:
        return ""
    import re
    s = str(raw).strip().upper()
    if len(s) == 2 and s.isalpha() and s not in ("CF", "IP", "MB", "MS"):
        return s
    KNOWN = {
        "HK": ["HONG", "HKG", "香港"],
        "TW": ["TAIWAN", "TPE", "台灣", "台湾"],
        "JP": ["JAPAN", "NRT", "KIX", "日本"],
        "SG": ["SINGAPORE", "SIN", "新加坡"],
        "KR": ["KOREA", "ICN", "韓國", "韩国"],
        "US": ["USA", "UNITED STATES", "SJC", "LAX", "美國", "美国", "圣何塞"],
        "DE": ["GERMANY", "FRA", "德國", "德国"],
        "NL": ["NETHERLANDS", "AMS", "荷蘭", "荷兰"],
        "GB": ["UK", "LONDON", "LHR", "英國", "英国"],
        "EU": ["EUROPE", "歐洲", "欧洲"],
    }
    for code, keywords in KNOWN.items():
        if s == code:
            return code
        for kw in keywords:
            if kw in s:
                return code
    words = re.findall(r"\b[A-Z]{2}\b", s)
    for w in words:
        if w not in ("CF", "IP", "MB", "MS", "BD"):
            return w
    return ""


def resolve_country_tag(item: Dict[str, Any], runner_trace_colo: str = "") -> str:
    """多層級決定最終輸出的 #COUNTRY 標籤，保證輸出標準雙字元大寫代碼：

    優先級 1: 情報源 / 種子庫在大陸境內實測的 source_colo (如 HKG, TPE, NRT, SIN)
    優先級 2: 情報源 / 種子庫明確標註的 source_country (如 #TW, #HK, #JP, #SG)
    優先級 3: 靜態網段字典 (CIDR -> Country 映射，繼承母體種子區域)
    優先級 4: 原生 country / countries 標註
    優先級 5: Runner 實測 Colo (若在台灣、日本、香港、新加坡則採計，美西僅做兜底)
    """
    ip = str(item.get("ip", "")).strip()
    source_colo = str(item.get("source_colo") or "").strip().upper()
    raw_source_country = item.get("source_country") or ""

    # 1. 優先採計境內探針實測 Colo
    if source_colo and source_colo != "DEFAULT":
        mapped = get_country_by_colo(source_colo)
        if mapped:
            return mapped

    # 2. 優先採計情報源/種子庫原生國家標籤 (經正規化)
    norm_tag = normalize_country_code(raw_source_country)
    if norm_tag:
        return norm_tag

    # 3. 檢查已知母體網段字典 (如 104.16.85.0/24#HK, 172.67.67.0/24#TW, 103.21.244.0/24#HK)
    subnet_tag = lookup_subnet_country(ip)
    if subnet_tag:
        norm_sub = normalize_country_code(subnet_tag)
        if norm_sub:
            return norm_sub

    # 4. 檢查 item["country"] 或 item["countries"] 原生標註
    orig_country = normalize_country_code(item.get("country"))
    if orig_country:
        return orig_country

    for c in item.get("countries", []):
        norm_c = normalize_country_code(c)
        if norm_c:
            return norm_c

    # 5. 若海外 Runner 測得亞太機房或歐洲機房，可採信 (如 TPE, HKG, NRT, SIN, FRA)
    runner_c = get_country_by_colo(runner_trace_colo)
    if runner_c in ("HK", "TW", "JP", "SG", "KR", "DE", "GB", "NL"):
        return runner_c

    # 6. 最後兜底：若為美國機房則標 US，否則標 CF
    if runner_c == "US":
        return "US"

    return "CF"

