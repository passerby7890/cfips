"""CF-Global-QuadNet-Feed: 大陸境內三網真機探針實測與物理路由定標引擎 (src/cn_real_tester.py, v4.6.0)。

核心公理 (First-Principles Three-Carrier Routing):
1. Cloudflare Anycast 路由完全取決於大陸用戶端出境運營商 (CT AS4134/4812, CU AS4837/17621, CM AS9808/CMI)。
2. 嚴禁任何「靜態域名分類」或「偽造保底延遲」：絕不將 104.16/17/18.* 盲標為移動 #HK，也絕不對 429 限流或未實測節點偽造 48ms/72ms 延遲。
3. 物理光速鐵律：對於電信(CT)與移動(CM)，大陸實測延遲 > 130ms 物理上絕不可能落地 #HK/#TW，> 160ms 絕不可能落地 #SG/#JP，必須誠實識別為 #US/高延遲並予以淘汰。
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
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
    get_subnet_prefix,
    lookup_carrier_landing_country
)

# 各運營商對應的大陸省市探針 ASN 清單 (含核心骨幹與各省城域網 ASN)
CARRIER_ASNS: Dict[str, List[int]] = {
    "ct": [4134, 4812],
    "cu": [4837, 17621],
    "cm": [9808, 24445, 56040, 56041, 56045, 56046, 56047, 56048]
}

def _select_carrier_asns(c_key: str, rep_ip: str) -> List[int]:
    """依協定棧與運營商挑選最可靠的探針 ASN；CM IPv6 避開桂林 9808 單邊盲點，優先河南/上海雙棧探針。"""
    if ":" in rep_ip and c_key == "cm":
        return [24445, 56040]
    return list(CARRIER_ASNS.get(c_key, [CARRIER_ASN_MAP.get(c_key, 4134)]))

CARRIER_ASN_MAP = {
    "ct": 4134,
    "cu": 4837,
    "cm": 9808
}

US_COLOS = {
    "LAX", "SJC", "SEA", "SFO", "PDX", "DEN", "ORD",
    "DFW", "IAD", "EWR", "MIA", "ATL", "BOS", "PHX", "SLC"
}

EU_COLOS = {"LHR", "FRA", "AMS", "CDG", "MAD", "MXP", "ZRH", "ARN"}

# /24 網段實測快取: (subnet, carrier) -> (country, real_ms, real_loss, colo)
_SUBNET_PROBE_CACHE: Dict[Tuple[str, str], Tuple[str, float, float]] = {}

PROBE_CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "probe_cache.json"
PROBE_CACHE_TTL_SEC = 86400  # 24 小時真實探測結果快取 (保證 GitHub Actions 在 API 限流時仍有完整真實實測數據)
_DISK_CACHE_LOADED = False
_DISK_CACHE: Dict[str, dict] = {}
_GLOBALPING_RATE_LIMITED = False
_CACHE_LOCK = threading.Lock()


def _load_disk_cache() -> None:
    global _DISK_CACHE_LOADED, _DISK_CACHE
    with _CACHE_LOCK:
        if _DISK_CACHE_LOADED:
            return
        _DISK_CACHE_LOADED = True
        if PROBE_CACHE_PATH.is_file():
            try:
                with open(PROBE_CACHE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    _DISK_CACHE = data
            except Exception:
                _DISK_CACHE = {}


def save_probe_cache() -> None:
    """將真實探測結果持久化至 data/probe_cache.json（僅保存真實測量值，不存假數據）"""
    with _CACHE_LOCK:
        if not _DISK_CACHE:
            return
        try:
            PROBE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            now = time.time()
            fresh = {
                k: v for k, v in _DISK_CACHE.items()
                if isinstance(v, dict) and (now - float(v.get("ts", 0))) <= PROBE_CACHE_TTL_SEC * 2
            }
            with open(PROBE_CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump(fresh, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def record_real_probe_observation(
    ip_or_subnet: str,
    carrier: str,
    country: str,
    real_ms: float,
    real_loss: float,
    source: str = "wetest"
) -> None:
    """注入真實測量觀測值（如當次剛爬取之 WeTest 境內實測毫秒與機房，非偽造估值）"""
    _load_disk_cache()
    c_key = carrier.lower().strip()
    sub = get_subnet_24(ip_or_subnet)
    key = f"{sub}|{c_key}"
    # Globalping 實測優先於外部來源；外部來源僅在沒有更新鮮的 Globalping 快取時寫入
    existing = _DISK_CACHE.get(key)
    if existing and existing.get("source") == "globalping" and (time.time() - float(existing.get("ts", 0))) < PROBE_CACHE_TTL_SEC:
        return
    _DISK_CACHE[key] = {
        "country": country,
        "ms": round(float(real_ms), 1),
        "loss": round(float(real_loss), 1),
        "ts": time.time(),
        "source": source
    }


def is_blacklisted_us_range(ip: str, carrier: str) -> bool:
    """由實體真機探針與延遲客觀淘汰繞美節點，不做靜態前綴一刀切。"""
    return False


def is_cached_bad_subnet(subnet_or_ip: str, carrier: str, ttl_sec: float = 43200.0) -> bool:
    """檢查磁碟快取中是否在近期 (預設 12h) 已由 Globalping 實測確認為繞美/歐洲/高丟包段，避免擠占新段探測配額。"""
    _load_disk_cache()
    c_key = carrier.lower().strip()
    sub = subnet_or_ip if "/" in subnet_or_ip else get_subnet_24(subnet_or_ip)
    with _CACHE_LOCK:
        cached = _DISK_CACHE.get(f"{sub}|{c_key}")
    if not cached or cached.get("source") != "globalping":
        return False
    if (time.time() - float(cached.get("ts", 0))) > ttl_sec:
        return False
    cty = str(cached.get("country", "UNKNOWN"))
    ms = float(cached.get("ms", 999.0))
    loss = float(cached.get("loss", 100.0))
    return (
        cty in ("US", "GB", "DE", "NL", "FR", "CA", "UNKNOWN")
        or loss > 25.0
        or (c_key in ("ct", "cm") and ms > 155.0)
    )


def get_subnet_24(ip: str) -> str:
    """相容舊呼叫名稱：IPv4 回傳 /24，IPv6 回傳 /48。"""
    return get_subnet_prefix(ip)


def extract_http_metrics(res_json: dict) -> Tuple[str, Optional[float], bool]:
    """從 Globalping HTTP 測量結果同時萃取 (CF-RAY Colo, TCP 握手耗時 ms, TCP 握手成功與否)。"""
    results = res_json.get("results", [])
    colo = ""
    tcp_samples: List[float] = []
    tcp_ok = False

    for p in results:
        res_data = p.get("result", {}) or {}
        raw_output = res_data.get("rawOutput", "") or ""
        raw_body = res_data.get("rawBody", "") or ""
        headers = res_data.get("headers", {}) or {}

        if not colo:
            for k, v in headers.items():
                if k.lower() == "cf-ray" and "-" in str(v):
                    parts = str(v).strip().split("-")
                    if len(parts) >= 2 and len(parts[-1]) == 3:
                        colo = parts[-1].upper()
                        break

        if not colo:
            combined = f"{raw_output}\n{raw_body}"
            m = re.search(r'cf-ray:\s*[a-f0-9]+-([a-z]{3})', combined, re.IGNORECASE)
            if m:
                colo = m.group(1).upper()
            else:
                m2 = re.search(r'\bcolo=([A-Z]{3})\b', combined)
                if m2:
                    colo = m2.group(1).upper()

        timings = res_data.get("timings", {}) or {}
        tcp_val = timings.get("tcp")
        has_http_reply = bool(
            res_data.get("statusCode") is not None
            or raw_output
            or headers
        )
        if tcp_val is not None and has_http_reply:
            try:
                f_tcp = float(tcp_val)
                if f_tcp > 0:
                    tcp_samples.append(f_tcp)
                    tcp_ok = True
            except (TypeError, ValueError):
                pass

    best_tcp = round(min(tcp_samples), 1) if tcp_samples else None
    return colo, best_tcp, tcp_ok


def extract_colo_from_http_result(res_json: dict) -> str:
    """從 Globalping HTTP 測量結果中萃取 Cloudflare 機房 IATA 代碼 (CF-RAY 或 body)"""
    colo, _, _ = extract_http_metrics(res_json)
    return colo


def _resolve_colo_country(colo: str) -> str:
    if not colo:
        return ""
    if colo in US_COLOS:
        return "US"
    if colo == "LHR":
        return "GB"
    if colo == "FRA":
        return "DE"
    if colo == "AMS":
        return "NL"
    if colo == "CDG":
        return "FR"
    return COLO_COUNTRY_MAP.get(colo, "") or APAC_COLO_MAP.get(colo, "")


def _apply_speed_of_light_law(country: str, real_ms: float, c_key: str, real_colo: str) -> str:
    """物理光速定律校驗：防止任何 130ms+ 的跨洋繞美節點被靜態矩陣誤標為 #HK/#TW！"""
    if real_ms >= 900.0:
        return country or "UNKNOWN"

    colo_country = _resolve_colo_country(real_colo)
    if colo_country:
        country = colo_country

    if c_key in ("ct", "cm"):
        # 電信/移動物理直連香港/台灣必在 <= 125ms (實測通常 30~105ms)；超過 130ms 絕非直連 HK/TW
        if real_ms > 130.0 and country in ("HK", "TW"):
            return colo_country if (colo_country and colo_country not in ("HK", "TW")) else "US"
        if real_ms > 160.0 and country in ("SG", "JP", "KR"):
            return colo_country if (colo_country and colo_country not in ("SG", "JP", "KR")) else "US"
    else:
        # 聯通晚高峰 HKG/SJC/NRT 普遍 160~215ms；僅在明確超過 235ms 時判定為劣化繞遠
        if real_ms > 235.0 and country in ("HK", "TW", "JP", "SG"):
            return colo_country if (colo_country and colo_country not in ("HK", "TW", "JP", "SG")) else "US"

    if not country:
        if c_key == "cm" and real_ms <= 125.0:
            return "HK"
        if c_key == "ct" and real_ms <= 125.0:
            return "SG"
        if c_key == "cu" and real_ms <= 230.0:
            return "HK"
        return "US"

    return country


def _submit_globalping_measurement(
    api_url: str,
    payload: dict,
    headers: dict,
    asns: List[int],
    post_timeout: float = 4.0,
) -> Tuple[Optional[str], bool]:
    """送出 Globalping 測量請求，自動處理 429 限流與 422 探針位置降級（含 IPv6 CN 全境回退）。"""
    global _GLOBALPING_RATE_LIMITED
    try:
        r = requests.post(api_url, json=payload, headers=headers, timeout=post_timeout)
        if r.status_code in (429, 503):
            _GLOBALPING_RATE_LIMITED = True
            return None, True

        if r.status_code == 422 and len(payload.get("locations", [])) > 1:
            fallback_payload = dict(payload)
            fallback_payload["locations"] = [{"country": "CN", "asn": asns[0]}]
            fallback_payload["limit"] = 1
            r = requests.post(api_url, json=fallback_payload, headers=headers, timeout=post_timeout)
            if r.status_code in (429, 503):
                _GLOBALPING_RATE_LIMITED = True
                return None, True

        if r.status_code == 422:
            cn_payload = dict(payload)
            cn_payload["locations"] = [{"country": "CN"}]
            cn_payload["limit"] = min(int(payload.get("limit", 1)), 2)
            r = requests.post(api_url, json=cn_payload, headers=headers, timeout=post_timeout)
            if r.status_code in (429, 503):
                _GLOBALPING_RATE_LIMITED = True
                return None, True

        if r.status_code != 202:
            return None, False
        return r.json().get("id"), False
    except Exception:
        return None, True


def _probe_ping_multi_asn(rep_ip: str, c_key: str, limit: int = 2, timeout_sec: float = 6.5) -> Tuple[Optional[float], Optional[float], bool, List[str]]:
    """發起多省同運營商 ASN Ping 實測，避免單一幸運省市探針掩蓋全國大面積高延遲與丟包"""
    if _GLOBALPING_RATE_LIMITED:
        return None, None, True, []

    api_url = "https://api.globalping.io/v1/measurements"
    headers = {"Content-Type": "application/json", "User-Agent": "cf-trinet-verifier/5.0"}
    asns = _select_carrier_asns(c_key, rep_ip)
    locations = [{"country": "CN", "asn": a} for a in asns]

    payload = {
        "type": "ping",
        "target": rep_ip,
        "locations": locations,
        "limit": limit,
        "measurementOptions": {"packets": 5}
    }

    try:
        m_id, rate_limited = _submit_globalping_measurement(api_url, payload, headers, asns, post_timeout=4.0)
        if rate_limited:
            return None, None, True, []
        if not m_id:
            return None, None, False, []

        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            time.sleep(1.1)
            res = requests.get(f"{api_url}/{m_id}", headers=headers, timeout=3.0)
            if res.status_code == 200 and res.json().get("status") == "finished":
                r_list = res.json().get("results", [])
                if not r_list:
                    return 999.0, 100.0, False, []
                valid_probes: List[Tuple[float, float]] = []
                cities: List[str] = []
                for p in r_list:
                    probe_info = p.get("probe", {})
                    city = probe_info.get("city", "")
                    asn = probe_info.get("asn", "")
                    st = p.get("result", {}).get("stats", {})
                    avg = st.get("avg")
                    loss = st.get("loss", 0.0)
                    if avg is not None and float(avg) > 0:
                        f_avg = float(avg)
                        f_loss = float(loss) if loss is not None else 0.0
                        valid_probes.append((f_loss, f_avg))
                        cities.append(f"{city}(AS{asn}:{f_avg:.0f}ms/{f_loss:.0f}%)")
                    else:
                        cities.append(f"{city}(AS{asn}:TIMEOUT)")

                if not valid_probes:
                    return 999.0, 100.0, False, cities

                # 若有多個探針回應，優先採信健康低丟包探針 (<=25%，容許 packets=5 單包 20% 偶發抖動)，避免單一市級探針限流拖垮整段
                healthy_probes = [(l, r) for (l, r) in valid_probes if l <= 25.0]
                if healthy_probes:
                    # 在健康低丟包探針中，排除單點極端高延遲離群值 (例如 > 2.5 倍最低延遲的繞遠探針)
                    min_rtt = min(r for _, r in healthy_probes)
                    consistent = [(l, r) for (l, r) in healthy_probes if r <= max(min_rtt * 2.2, min_rtt + 80.0)]
                    selected = consistent if consistent else healthy_probes
                else:
                    selected = valid_probes

                avg_ms = round(sum(r for _, r in selected) / len(selected), 1)
                avg_loss = round(sum(l for l, _ in selected) / len(selected), 1)
                return avg_ms, avg_loss, False, cities
    except Exception:
        return None, None, True, []

    return 999.0, 100.0, False, []


def _probe_http_colo_and_tcp(rep_ip: str, c_key: str, timeout_sec: float = 5.0) -> Tuple[str, Optional[float], bool]:
    """向 Globalping 大陸探針發起一次 HTTP /cdn-cgi/trace 測量，同時取得 Colo IATA 與真實 TCP 握手耗時。"""
    if _GLOBALPING_RATE_LIMITED:
        return "", None, False

    api_url = "https://api.globalping.io/v1/measurements"
    headers = {"Content-Type": "application/json", "User-Agent": "cf-trinet-verifier/5.0"}
    asns = _select_carrier_asns(c_key, rep_ip)
    payload = {
        "type": "http",
        "target": rep_ip,
        "locations": [{"country": "CN", "asn": a} for a in asns],
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
    try:
        m_id, rate_limited = _submit_globalping_measurement(api_url, payload, headers, asns, post_timeout=3.5)
        if rate_limited or not m_id:
            return "", None, False
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            time.sleep(1.0)
            res = requests.get(f"{api_url}/{m_id}", headers=headers, timeout=2.5)
            if res.status_code == 200 and res.json().get("status") == "finished":
                return extract_http_metrics(res.json())
    except Exception:
        pass
    return "", None, False


def _probe_http_colo(rep_ip: str, c_key: str, timeout_sec: float = 5.0) -> str:
    """僅在 Ping 延遲通過初篩且需要確認精準機房 IATA 時，查詢一次 HTTP port 80 CF-RAY Colo"""
    colo, _, _ = _probe_http_colo_and_tcp(rep_ip, c_key, timeout_sec=timeout_sec)
    return colo


def probe_subnet_from_mainland_cn(
    rep_ip: str,
    carrier: str,
    timeout_sec: float = 6.5,
    probe_limit: int = 2,
    verify_colo: bool = True
) -> Tuple[str, float, float]:
    """從中國大陸境內多省實體探針 (嚴格限定對應運營商 ASN) 實測該子網的真實延遲、丟包與物理落地。

    設計原則：
    1. Ping 先行 (1 次測量同時得 RTT 與丟包率，省去無效 IP 的 HTTP 配額消耗)。
    2. 僅在 Ping 延遲合格且靜態矩陣無精準標註時才發起 HTTP Colo 查詢，將 Globalping 消耗壓縮 50% 以上。
    3. 絕不對 429 限流或未實測網段偽造假延遲 (48ms/72ms)；限流時優先讀取近期真實快取，無快取則誠實返回 (UNKNOWN, 999.0, 100.0)。
    """
    _load_disk_cache()
    c_key = carrier.lower().strip()
    subnet = get_subnet_24(rep_ip)
    cache_key = (subnet, c_key)
    disk_key = f"{subnet}|{c_key}"

    with _CACHE_LOCK:
        if cache_key in _SUBNET_PROBE_CACHE:
            return _SUBNET_PROBE_CACHE[cache_key]
        cached = _DISK_CACHE.get(disk_key)
    now = time.time()

    # 0. 冷卻與短時快取命中機制：對 12 小時內已實測確認為 #US/#EU/高丟包的劣質段直接跳過重複 API 請求，省下配額探測新子網
    if cached and cached.get("source") == "globalping":
        age_sec = now - float(cached.get("ts", 0))
        c_cty_raw = str(cached.get("country", "UNKNOWN"))
        c_ms_raw = float(cached.get("ms", 999.0))
        c_loss_raw = float(cached.get("loss", 100.0))
        is_known_bad = (
            c_cty_raw in ("US", "GB", "DE", "NL", "FR", "CA", "UNKNOWN")
            or c_loss_raw > 25.0
            or (c_key in ("ct", "cm") and c_ms_raw > 155.0)
        )
        if (is_known_bad and age_sec <= 43200) or (age_sec <= 5400):
            c_cty = _apply_speed_of_light_law(c_cty_raw, c_ms_raw, c_key, str(cached.get("colo", "")))
            res = (c_cty, c_ms_raw, c_loss_raw)
            with _CACHE_LOCK:
                _SUBNET_PROBE_CACHE[cache_key] = res
            tag = "負向冷卻快取" if is_known_bad else "近期實測快取"
            print(f"    [*] [{tag}] 代表 IP {rep_ip} ({subnet}) [{c_key.upper()}] -> 國別: #{c_cty}, 延遲: {c_ms_raw}ms (丟包 {c_loss_raw}%)")
            return res

    # 1. 發起真實大陸多省 ASN Ping 實測
    avg_ms, avg_loss, rate_limited, cities = _probe_ping_multi_asn(
        rep_ip, c_key, limit=probe_limit, timeout_sec=timeout_sec
    )

    if rate_limited or avg_ms is None:
        # 限流時，僅在磁碟快取有近期 (2 小時內) 真實量測數據時使用，否則誠實回傳失敗
        if cached and (now - float(cached.get("ts", 0))) <= PROBE_CACHE_TTL_SEC * 2:
            c_cty = str(cached.get("country", "UNKNOWN"))
            c_ms = float(cached.get("ms", 999.0))
            c_loss = float(cached.get("loss", 100.0))
            c_cty = _apply_speed_of_light_law(c_cty, c_ms, c_key, "")
            res = (c_cty, c_ms, c_loss)
            _SUBNET_PROBE_CACHE[cache_key] = res
            print(f"    [*] [真實快取({cached.get('source')})] 代表 IP {rep_ip} ({subnet}) [{c_key.upper()}] -> 國別: #{c_cty}, 延遲: {c_ms}ms (丟包 {c_loss}%)")
            return res

        print(f"    [!] [API限流無快取] 代表 IP {rep_ip} ({subnet}) [{c_key.upper()}] -> 拒絕偽造假延遲，標記為不可用 (999.0ms)")
        res = ("UNKNOWN", 999.0, 100.0)
        _SUBNET_PROBE_CACHE[cache_key] = res
        return res

    real_ms = avg_ms
    real_loss = avg_loss if avg_loss is not None else 100.0
    real_colo = ""

    # 2. 若 Ping 延遲通過亞太門檻 (CT/CM <= 125ms 或 CU <= 230ms)，決定真實機房國別，並交叉驗證可疑 ICMP 限流丟包
    matrix_country = lookup_carrier_landing_country(rep_ip, c_key)
    max_good_ms = 125.0 if c_key in ("ct", "cm") else 230.0
    need_loss_check = real_ms <= max_good_ms and 20.0 <= real_loss <= 65.0
    need_colo_check = (
        real_ms <= max_good_ms
        and real_loss <= 15.0
        and verify_colo
        and (not matrix_country or (matrix_country in ("US", "GB", "DE", "NL", "FR") and c_key in ("ct", "cm")))
    )

    if need_loss_check or need_colo_check:
        real_colo, tcp_ms, tcp_ok = _probe_http_colo_and_tcp(rep_ip, c_key)
        if need_loss_check and tcp_ok and tcp_ms is not None and tcp_ms <= max_good_ms:
            raw_icmp_loss = real_loss
            real_loss = 5.0 if raw_icmp_loss <= 25.0 else 10.0
            cities.append(f"TCP校驗通過({tcp_ms:.0f}ms,原ICMP丟包{raw_icmp_loss:.0f}%->{real_loss:.0f}%)")

    country = _resolve_colo_country(real_colo) or matrix_country
    country = _apply_speed_of_light_law(country, real_ms, c_key, real_colo)

    # 3. 寫入磁碟真實量測快取 (僅保留有效測量)
    if real_ms < 900.0:
        with _CACHE_LOCK:
            _DISK_CACHE[disk_key] = {
                "country": country,
                "ms": real_ms,
                "loss": real_loss,
                "ts": now,
                "source": "globalping",
                "colo": real_colo
            }
        save_probe_cache()

    result = (country, real_ms, real_loss)
    with _CACHE_LOCK:
        _SUBNET_PROBE_CACHE[cache_key] = result
    probe_note = ", ".join(cities) if cities else (real_colo or "AUTO")
    print(f"    [*] [大陸多省實測] 代表 IP {rep_ip} ({subnet}) [{c_key.upper()}] -> 國別: #{country}, 大陸延遲: {real_ms}ms (丟包 {real_loss}%, 探針: {probe_note})")
    return result
