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


# 三網獨立 Anycast 物理落地矩陣快取 (CT / CU / CM 各自獨立)
_CARRIER_LANDING_MAP: dict[str, list[tuple[ipaddress.IPv4Network, str]]] = {"ct": [], "cu": [], "cm": []}


def init_carrier_landing_matrix(matrix_path: Any = None) -> dict[str, list[tuple[ipaddress.IPv4Network, str]]]:
    """載入 data/carrier_landing_matrix.json，建構三網獨立物理落地網段表"""
    global _CARRIER_LANDING_MAP
    import json
    from pathlib import Path
    if matrix_path is None:
        matrix_path = Path(__file__).resolve().parent.parent / "data" / "carrier_landing_matrix.json"
    else:
        matrix_path = Path(matrix_path)

    if matrix_path.is_file():
        try:
            with open(matrix_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            carriers = data.get("carriers", {})
            for c_code in ["ct", "cu", "cm"]:
                c_rules = carriers.get(c_code, {}).get("landing_rules", {})
                res = []
                for country_code, cidrs in c_rules.items():
                    for cidr in cidrs:
                        try:
                            net = ipaddress.ip_network(cidr.strip(), strict=False)
                            res.append((net, country_code.upper()))
                        except Exception:
                            pass
                res.sort(key=lambda x: x[0].prefixlen, reverse=True)
                _CARRIER_LANDING_MAP[c_code] = res
        except Exception:
            pass
    return _CARRIER_LANDING_MAP


def lookup_carrier_landing_country(ip_str: str, carrier: str) -> str:
    """由運營商 (ct/cu/cm) 與 IP 查詢真實海纜與 BGP 互聯物理落地國家代碼"""
    if not ip_str or not carrier:
        return ""
    c_code = carrier.lower().strip()
    if not _CARRIER_LANDING_MAP.get(c_code):
        init_carrier_landing_matrix()
    rules = _CARRIER_LANDING_MAP.get(c_code, [])
    try:
        ip_obj = ipaddress.IPv4Address(ip_str.strip())
        for net, country in rules:
            if ip_obj in net:
                return country
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


def resolve_country_tag(item: Dict[str, Any], runner_trace_colo: str = "", carrier: str = "") -> str:
    """多層級決定最終輸出的 #COUNTRY 標籤，保證 100% 符應運營商物理出境海纜與互聯點：

    核心原則：
    1. 運營商 (Carrier) 專屬 BGP Anycast 物理路由矩陣為最高依據 (如電信 108.162.198.* 走東京 JP，172.64.144.* 走新加坡 SG)
    2. 嚴禁採信 Runner (GitHub Actions / 本地 PC) 測得的 colo，因為海外或本地機器連線 Anycast 與大陸三網完全不同
    3. 中國移動 (CM): CMI 骨幹將 104.16~20.* 與 104.26.* 全數拉回香港 Mega-i，標註 #HK
    4. 中國電信 (CT) 與 中國聯通 (CU): 凡未在亞太直連海纜白名單內之普通 Cloudflare 網段 (如 104.16.*, 104.17.*, 104.18.*, 104.20.*)，100% 繞美，標註 #US
    """
    ip = str(item.get("ip", "")).strip()
    domain = str(item.get("domain") or "").lower()
    target_carrier = (carrier or str(item.get("carrier") or item.get("target_carrier") or "")).lower()

    # 1. 運營商專屬 BGP Anycast 物理路由矩陣查詢 (最高優先級)
    if target_carrier in ("ct", "cu", "cm"):
        matrix_country = lookup_carrier_landing_country(ip, target_carrier)
        if matrix_country:
            return matrix_country

    # 2. 針對中國移動 (CM): CMI 國際骨幹將流量全數匯聚於香港 Mega-i
    if target_carrier == "cm":
        if ip.startswith(("104.16.", "104.17.", "104.18.", "104.19.", "104.20.", "104.26.")):
            return "HK"

    # 3. 針對中國電信 (CT) 與 中國聯通 (CU):
    # 普通免費/Pro 段 (104.16~25.*, 172.67.*, 104.21.*) 在電信 163 與聯通 169 上全部跨太平洋走西海岸 (SJC/LAX) 繞美
    if target_carrier in ("ct", "cu"):
        if ip.startswith(("104.16.", "104.17.", "104.18.", "104.19.", "104.20.", "104.21.", "104.25.", "172.67.")):
            return "US"

    # 4. 網域命名特徵指紋 (如 ctsg.cfyx -> SG)
    if "ctsg" in domain or "singapore" in domain:
        return "SG"

    # 5. 檢查已知母體網段字典 (由 data/countries/ 載入)
    subnet_tag = lookup_subnet_country(ip)
    if subnet_tag:
        norm_sub = normalize_country_code(subnet_tag)
        if norm_sub and norm_sub not in ("CF", "UNKNOWN"):
            # 若為電信或聯通，非 Enterprise 網段仍不得誤標亞太
            if target_carrier in ("ct", "cu") and norm_sub in ("TW", "HK") and ip.startswith(("104.16.", "104.17.", "104.18.", "104.19.", "104.20.", "104.21.")):
                return "US"
            return norm_sub

    # 6. 兜底判定：絕不輸出 #CF
    if target_carrier == "cm":
        return "HK"
    elif target_carrier in ("ct", "cu"):
        return "US"

    return "HK"

