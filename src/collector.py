"""階段一：多維度情報收集器 (v1.1.0)。

實作四大收集策略：
- 策略 A：基於北上廣四網錨點的 ECS (EDNS Client Subnet) Google DoH 偽裝解析
- 策略 B：大陸境內公開三網實測榜單抓取 (HostMonit, WeTest, uouin 等)
- 策略 C：分國家地區公開優選/反代來源聚合
- 策略 D：本地精選種子庫補充 (支援 IP#COUNTRY 與 CIDR#COUNTRY 格式)
- 嚴格保留 source_country 與 source_colo 原生標籤，防止被海外 Runner 誤判覆蓋。
"""

from __future__ import annotations

import asyncio
import csv
import html
import ipaddress
import json
import logging
import random
import re
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
import requests

from .region_mapper import get_country_by_colo

log = logging.getLogger(__name__)

# Cloudflare 官方 IPv4 段回退清單（https://www.cloudflare.com/ips-v4）
CLOUDFLARE_IPV4_FALLBACK = [
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
    "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
]
CLOUDFLARE_IPS_URL = "https://www.cloudflare.com/ips-v4"

IPV4_RE = re.compile(
    r"(?<![\d.])((?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?:\.(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3})(?![\d.])"
)
TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.I | re.S)
TD_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")
LATENCY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:ms|毫秒)", re.I)
COLO_CELL_RE = re.compile(r"^[A-Z]{3}$")

# 頻寬與丟包率硬性門檻
MAX_LOSS_RATE = 1.0        # 丟包率必須 <= 1.0%
MIN_DOWNLOAD_SPEED = 20.0  # 實測下載頻寬必須 >= 20.0 Mbps

LOSS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%", re.I)
SPEED_KBPS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:kbps|kb/s|kb)", re.I)


def extract_speed_mbps(text: str) -> float | None:
    """從文字中萃取下載速率並統一轉換為 Mbps。"""
    if not text:
        return None
    m_byte = re.search(r"(\d+(?:\.\d+)?)\s*(?:mb/s|mbyte/s|mbps|mb|兆)", text, re.I)
    if m_byte:
        val = float(m_byte.group(1))
        if "mb/s" in text.lower():
            return val * 8.0
        return val
    m_k = SPEED_KBPS_RE.search(text)
    if m_k:
        return (float(m_k.group(1)) * 8.0) / 1000.0
    return None


def extract_loss_rate(text: str) -> float | None:
    """從文字中萃取丟包率 (%)。"""
    if not text:
        return None
    m = LOSS_RE.search(text)
    if m:
        return float(m.group(1))
    return None


CARRIER_KEYWORDS = {
    "ct": ("电信", "電信", "telecom", "189"),
    "cu": ("联通", "聯通", "unicom", "10010"),
    "cm": ("移动", "移動", "mobile", "10086"),
    "edu": ("教育", "cernet", "edu"),
}
HOSTMONIT_LINES = {"CT": "ct", "CU": "cu", "CM": "cm", "EDU": "edu"}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 cf-global-quadnet-feed/2.1"
)


@dataclass
class Candidate:
    ip: str
    carriers: set[str] = field(default_factory=set)      # {"ct", "cu", "cm", "edu"}
    countries: set[str] = field(default_factory=set)     # {"hk", "tw", "jp", ...}
    sources: set[str] = field(default_factory=set)       # {"ecs:gz_ct", "wetest", ...}
    is_shifted_neighbor: bool = False
    loss_rate: float = 0.0                               # 丟包率百分比 (%)
    download_speed: float = 50.0                         # 實測下載頻寬 (Mbps)
    source_country: str = ""                             # 來源綁定之大寫國家標籤 (例: TW, HK, JP, SG)
    source_colo: str = ""                                # 來源實測之機房標籤 (例: HKG, TPE, NRT, SJC)
    source_carrier: str = ""                             # 境內探針指定運營商 (ct / cu / cm)
    cn_latency_ms: float | None = None                   # 境內實測延遲 (ms)
    cn_speed_mbps: float = 0.0                           # 境內實測頻寬 (Mbps)
    cn_loss_percent: float = 0.0                         # 境內丟包率 (%)
    source_hint: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        c_tag = self.source_country.lower() if self.source_country else (list(self.countries)[0] if self.countries else "")
        return {
            "ip": self.ip,
            "carriers": sorted(self.carriers),
            "countries": sorted(self.countries),
            "sources": sorted(self.sources),
            "is_shifted_neighbor": self.is_shifted_neighbor,
            "loss_rate": self.loss_rate,
            "download_speed": self.download_speed,
            "source_country": self.source_country,
            "source_colo": self.source_colo,
            "source_carrier": self.source_carrier,
            "cn_latency_ms": self.cn_latency_ms,
            "cn_speed_mbps": self.cn_speed_mbps,
            "cn_loss_percent": self.cn_loss_percent,
            "country": c_tag,
            "country_code": self.source_country,
            "colo": self.source_colo or None,
            "source_hint": dict(self.source_hint),
        }


def _strip(fragment: str) -> str:
    return html.unescape(TAG_RE.sub(" ", fragment)).strip()


def _detect_carrier(text: str) -> str | None:
    if not text:
        return None
    raw = str(text).strip()
    upper = raw.upper()
    if upper in HOSTMONIT_LINES:
        return HOSTMONIT_LINES[upper]
    for key, words in CARRIER_KEYWORDS.items():
        if any(w in raw for w in words):
            return key
    for code, key in HOSTMONIT_LINES.items():
        if re.search(r"(?:\b|_)" + re.escape(code) + r"(?:\b|_)", upper):
            return key
    return None


async def fetch_cloudflare_ranges(session: aiohttp.ClientSession | None) -> list[ipaddress.IPv4Network]:
    nets = [ipaddress.IPv4Network(n) for n in CLOUDFLARE_IPV4_FALLBACK]
    if session is None:
        return nets
    try:
        async with session.get(CLOUDFLARE_IPS_URL, timeout=aiohttp.ClientTimeout(total=8.0)) as resp:
            resp.raise_for_status()
            text = await resp.text()
            extracted: list[ipaddress.IPv4Network] = []
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    net = ipaddress.ip_network(line, strict=False)
                    if net.version == 4:
                        extracted.append(net)
                except ValueError:
                    continue
            if extracted:
                return extracted
    except Exception as exc:
        log.warning("取得 Cloudflare 官方 IP 段失敗，使用內建清單：%s", exc)
    return nets


def in_networks(ip: str, nets: list[ipaddress.IPv4Network]) -> bool:
    try:
        addr = ipaddress.IPv4Address(ip)
        return any(addr in n for n in nets)
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# 策略 A: ECS DoH 偽裝解析
# --------------------------------------------------------------------------- #
async def _resolve_anchor_ip(anchor: dict[str, Any]) -> str:
    """優先解析動態維護域名，失敗時回退至靜態 IPv4。"""
    domain = anchor.get("domain")
    if domain:
        try:
            loop = asyncio.get_running_loop()
            res = await loop.getaddrinfo(domain, None, family=socket.AF_INET)
            if res and res[0][4][0]:
                return res[0][4][0]
        except Exception:
            pass
    return str(anchor.get("ip") or anchor.get("ipv4", "")).strip()


async def collect_ecs_doh(
    session: aiohttp.ClientSession,
    cfg: dict[str, Any],
    anchors: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """以北上廣四網錨點對目標域名發起 Google DoH ECS 查詢。"""
    if not cfg.get("enabled", True):
        return []

    doh_url = cfg.get("doh_url", "https://dns.google/resolve")
    domains = cfg.get("domains", [])
    results: list[dict[str, Any]] = []

    anchor_ips = await asyncio.gather(*(_resolve_anchor_ip(a) for a in anchors))

    async def _query(domain: str, anchor: dict[str, Any], ip: str) -> list[dict[str, Any]]:
        if not ip:
            return []
        carrier = anchor.get("carrier", "")
        code = anchor.get("code", "")
        params = {
            "name": domain,
            "type": "A",
            "edns_client_subnet": f"{ip}/24",
        }
        out = []
        try:
            async with session.get(doh_url, params=params, timeout=aiohttp.ClientTimeout(total=8.0)) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                for ans in data.get("Answer", []):
                    if ans.get("type") == 1:
                        target_ip = str(ans.get("data", "")).strip()
                        if IPV4_RE.fullmatch(target_ip):
                            c_tag = None
                            if "visa.hk" in domain or domain.endswith(".hk") or "visa.com.hk" in domain:
                                c_tag = "hk"
                            elif "visa.com.sg" in domain or domain.endswith(".sg"):
                                c_tag = "sg"
                            elif domain.endswith(".tw") or "visa.com.tw" in domain:
                                c_tag = "tw"
                            elif domain.endswith(".jp") or "visa.co.jp" in domain:
                                c_tag = "jp"

                            item = {
                                "ip": target_ip,
                                "carrier": carrier,
                                "source": f"ecs:{code}",
                                "domain": domain,
                            }
                            if c_tag:
                                item["country"] = c_tag
                                item["source_country"] = c_tag.upper()
                            out.append(item)
        except Exception as exc:
            log.debug("ECS DoH 查詢失敗 [%s / %s]: %s", domain, code, exc)
        return out

    tasks = []
    for domain in domains:
        for anchor, ip in zip(anchors, anchor_ips):
            tasks.append(_query(domain, anchor, ip))

    nested = await asyncio.gather(*tasks, return_exceptions=True)
    for res in nested:
        if isinstance(res, list):
            results.extend(res)

    log.info("策略 A (ECS DoH) 成功獲取 %d 筆入口記錄", len(results))
    return results


# --------------------------------------------------------------------------- #
# 策略 B: 大陸境內公開三網實測榜單抓取
# --------------------------------------------------------------------------- #
def parse_html_table(body: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for tr in TR_RE.findall(body):
        cells = [_strip(td) for td in TD_RE.findall(tr)]
        row_text = " ".join(cells) if cells else _strip(tr)
        m = IPV4_RE.search(row_text)
        if not m:
            continue
        colo = next((c for c in cells if COLO_CELL_RE.match(c)), None)
        lat = LATENCY_RE.search(row_text)

        loss_rate = extract_loss_rate(row_text)
        speed_mbps = extract_speed_mbps(row_text)
        if "5 mb" in row_text.lower() and (speed_mbps is None or speed_mbps < 40.0):
            speed_mbps = 40.0

        if loss_rate is not None and loss_rate > MAX_LOSS_RATE:
            continue
        if speed_mbps is not None and speed_mbps < MIN_DOWNLOAD_SPEED:
            continue

        valid_colo = str(colo).upper() if colo and COLO_CELL_RE.match(colo) else ""
        source_country = get_country_by_colo(valid_colo) if valid_colo else ""

        # 檢測中文地區關鍵字
        if not source_country:
            if any(w in row_text for w in ("香港", "HK")):
                source_country = "HK"
            elif any(w in row_text for w in ("台湾", "台灣", "TW")):
                source_country = "TW"
            elif any(w in row_text for w in ("日本", "JP")):
                source_country = "JP"
            elif any(w in row_text for w in ("新加坡", "SG")):
                source_country = "SG"
            elif any(w in row_text for w in ("韩国", "韓國", "KR")):
                source_country = "KR"
            elif any(w in row_text for w in ("美国", "美國", "US")):
                source_country = "US"

        c_det = _detect_carrier(row_text)
        lat_val = float(lat.group(1)) if lat else None
        rows.append({
            "ip": m.group(1),
            "carrier": c_det,
            "source_carrier": c_det if c_det in ("ct", "cu", "cm") else "",
            "cn_latency_ms": lat_val,
            "cn_speed_mbps": speed_mbps if speed_mbps is not None else 50.0,
            "cn_loss_percent": loss_rate if loss_rate is not None else 0.0,
            "colo": valid_colo or None,
            "source_colo": valid_colo,
            "source_country": source_country,
            "country": source_country.lower() if source_country else None,
            "latency_ms": lat_val,
            "loss_rate": loss_rate if loss_rate is not None else 0.0,
            "download_speed": speed_mbps if speed_mbps is not None else 50.0,
        })
    return rows


def parse_hostmonit(payload: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    info = payload.get("info") if isinstance(payload, dict) else None
    if isinstance(info, dict):
        info = [dict(item, line=line) for line, items in info.items() for item in (items or [])]
    for item in info or []:
        if not isinstance(item, dict) or not IPV4_RE.fullmatch(str(item.get("ip", ""))):
            continue
        colo = str(item.get("colo") or "").upper()
        lat = item.get("latency")

        loss_raw = item.get("loss", 0.0)
        speed_raw = item.get("speed", 50.0)
        loss_rate = float(loss_raw) if isinstance(loss_raw, (int, float)) else 0.0
        speed_mbps = float(speed_raw) if isinstance(speed_raw, (int, float)) else 50.0

        if loss_rate > MAX_LOSS_RATE:
            continue
        if speed_mbps < MIN_DOWNLOAD_SPEED:
            continue

        valid_colo = colo if (COLO_CELL_RE.match(colo) and colo != "DEFAULT") else ""
        source_country = get_country_by_colo(valid_colo) if valid_colo else ""

        line = str(item.get("line", "")).upper()
        node = str(item.get("node", "")).upper()
        carrier = HOSTMONIT_LINES.get(line)
        if not carrier:
            if "CM" in node or "MOBILE" in node:
                carrier = "cm"
            elif "CU" in node or "UNICOM" in node:
                carrier = "cu"
            elif "CT" in node or "TELECOM" in node:
                carrier = "ct"
            else:
                carrier = "all"

        lat_val = float(lat) if isinstance(lat, (int, float)) else None
        rows.append({
            "ip": item["ip"],
            "carrier": carrier,
            "source_carrier": carrier if carrier in ("ct", "cu", "cm") else "",
            "cn_latency_ms": lat_val,
            "cn_speed_mbps": speed_mbps,
            "cn_loss_percent": loss_rate,
            "colo": valid_colo or None,
            "source_colo": valid_colo,
            "source_country": source_country,
            "country": source_country.lower() if source_country else None,
            "latency_ms": lat_val,
            "loss_rate": loss_rate,
            "download_speed": speed_mbps,
        })
    return rows


def parse_cdtools(body: str, source_name: str = "cdtools") -> list[dict[str, Any]]:
    """解析 cf-ip.cdtools.click 境內即時測速榜 (北上深蓉真實探針)"""
    rows: list[dict[str, Any]] = []
    tr_matches = TR_RE.findall(body)
    for tr in tr_matches:
        cells = [_strip(td) for td in TD_RE.findall(tr)]
        if len(cells) >= 5:
            m = IPV4_RE.search(cells[0])
            if not m:
                continue
            ip = m.group(1)
            loss_rate = extract_loss_rate(cells[3]) or 0.0
            lat_str = cells[4].replace("ms", "").strip()
            try:
                lat = float(lat_str)
            except Exception:
                lat = 60.0
            spd_str = cells[5] if len(cells) > 5 else "50"
            spd = extract_speed_mbps(spd_str) or 50.0
            if loss_rate <= MAX_LOSS_RATE:
                rows.append({
                    "ip": ip,
                    "carrier": "all",
                    "source": source_name,
                    "source_carrier": "all",
                    "cn_latency_ms": lat,
                    "cn_speed_mbps": spd,
                    "cn_loss_percent": loss_rate,
                    "latency_ms": lat,
                    "loss_rate": loss_rate,
                    "download_speed": spd,
                })
    return rows


def parse_text(body: str) -> list[dict[str, Any]]:
    """支援解析 172.67.140.251#TW 或 104.28.16.0/24#HK 等帶有國家標註之文字行"""
    rows: list[dict[str, Any]] = []
    seen = set()
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        tag_country = ""
        if "#" in line:
            parts = line.split("#", 1)
            line = parts[0].strip()
            tag_country = parts[1].strip().upper()

        if ":" in line and line.count(":") == 1:
            line = line.split(":", 1)[0].strip()

        m = IPV4_RE.search(line)
        if m:
            ip = m.group(1)
            if ip not in seen:
                seen.add(ip)
                rows.append({
                    "ip": ip,
                    "carrier": "all",
                    "colo": None,
                    "source_colo": "",
                    "source_country": tag_country,
                    "country": tag_country.lower() if tag_country else None,
                    "latency_ms": None,
                    "loss_rate": 0.0,
                    "download_speed": 50.0,
                })
    return rows


def parse_json_feed(payload: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        for wrap in ("info", "data", "results", "ips"):
            if wrap in payload and isinstance(payload[wrap], (list, dict)):
                return parse_json_feed(payload[wrap])
        for k, v in payload.items():
            carrier = _detect_carrier(k)
            if isinstance(v, list):
                for sub in v:
                    for r in parse_json_feed(sub):
                        if carrier and not r.get("carrier"):
                            r["carrier"] = carrier
                        rows.append(r)
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, str) and IPV4_RE.fullmatch(item.strip()):
                rows.append({
                    "ip": item.strip(),
                    "carrier": None,
                    "colo": None,
                    "source_colo": "",
                    "source_country": "",
                    "latency_ms": None,
                    "loss_rate": 0.0,
                    "download_speed": 50.0,
                })
            elif isinstance(item, dict):
                ip = str(item.get("ip") or item.get("address") or "").strip()
                if IPV4_RE.fullmatch(ip):
                    c = _detect_carrier(str(item.get("carrier") or item.get("line") or ""))
                    colo = str(item.get("colo") or "").upper()
                    lat = item.get("latency") or item.get("delay")

                    loss = item.get("loss") or item.get("loss_rate") or item.get("packet_loss")
                    speed = item.get("speed") or item.get("download_speed") or item.get("bandwidth")
                    loss_f = float(loss) if isinstance(loss, (int, float)) else extract_loss_rate(str(loss)) if loss else None
                    speed_f = float(speed) if isinstance(speed, (int, float)) else extract_speed_mbps(str(speed)) if speed else None

                    if loss_f is not None and loss_f > MAX_LOSS_RATE:
                        continue
                    if speed_f is not None and speed_f < MIN_DOWNLOAD_SPEED:
                        continue

                    valid_colo = colo if COLO_CELL_RE.match(colo) else ""
                    source_country = get_country_by_colo(valid_colo) if valid_colo else ""

                    rows.append({
                        "ip": ip,
                        "carrier": c,
                        "colo": valid_colo or None,
                        "source_colo": valid_colo,
                        "source_country": source_country,
                        "country": source_country.lower() if source_country else None,
                        "latency_ms": float(lat) if isinstance(lat, (int, float)) else None,
                        "loss_rate": loss_f if loss_f is not None else 0.0,
                        "download_speed": speed_f if speed_f is not None else 50.0,
                    })
    return rows


async def _fetch_public_source(session: aiohttp.ClientSession, src: dict[str, Any]) -> list[dict[str, Any]]:
    name = src.get("name", "unknown")
    stype = src.get("type", "text")
    url = src.get("url", "")
    key = src.get("key", "")
    try:
        if stype == "hostmonit":
            try:
                async with session.post(url, json={"key": key or "iDetkOys"}, timeout=aiohttp.ClientTimeout(total=10.0)) as resp:
                    resp.raise_for_status()
                    payload = json.loads(await resp.read())
                    return parse_hostmonit(payload) or parse_json_feed(payload)
            except Exception:
                async with session.get(url, params={"key": key or "iDetkOys"}, timeout=aiohttp.ClientTimeout(total=10.0)) as resp:
                    resp.raise_for_status()
                    payload = json.loads(await resp.read())
                    return parse_hostmonit(payload) or parse_json_feed(payload)
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=12.0)) as resp:
            resp.raise_for_status()
            data = await resp.read()
            if stype == "json":
                return parse_json_feed(json.loads(data))
            body = data.decode("utf-8", errors="replace")
            if stype == "cdtools":
                return parse_cdtools(body, name)
            if stype == "html_table":
                return parse_html_table(body)
            return parse_text(body)
    except Exception as exc:
        log.warning("抓取公開數據源 %s 失敗：%s", name, exc)
    return []


async def collect_mainland_sources(
    session: aiohttp.ClientSession,
    cfg: dict[str, Any],
) -> list[dict[str, Any]]:
    if not cfg.get("enabled", True):
        return []
    sources = cfg.get("sources", [])
    results: list[dict[str, Any]] = []
    nested = await asyncio.gather(*(_fetch_public_source(session, s) for s in sources), return_exceptions=True)
    for src_def, rows in zip(sources, nested):
        if isinstance(rows, list):
            for r in rows:
                r["source"] = src_def.get("name", "mainland")
                results.append(r)
    log.info("策略 B (境內實測榜單) 成功獲取 %d 筆記錄", len(results))
    return results


# --------------------------------------------------------------------------- #
# 策略 C: 分國家地區公開來源
# --------------------------------------------------------------------------- #
async def collect_country_sources(
    session: aiohttp.ClientSession,
    cfg: dict[str, Any],
) -> list[dict[str, Any]]:
    if not cfg.get("enabled", True):
        return []
    sources = cfg.get("sources", [])
    results: list[dict[str, Any]] = []
    nested = await asyncio.gather(*(_fetch_public_source(session, s) for s in sources), return_exceptions=True)
    for src_def, rows in zip(sources, nested):
        c_code = str(src_def.get("country", "")).strip()
        if isinstance(rows, list):
            for r in rows:
                r["source"] = src_def.get("name", "country")
                r["carrier"] = "all"  # 確保分國優選可供三網平等競爭
                if c_code:
                    r["country"] = c_code.lower()
                    r["source_country"] = c_code.upper()
                results.append(r)
    log.info("策略 C (分國來源) 成功獲取 %d 筆記錄", len(results))
    return results


# --------------------------------------------------------------------------- #
# 策略 D: 本地精選種子庫補充
# --------------------------------------------------------------------------- #
def sample_network(net: ipaddress.IPv4Network, count: int, rng: random.Random) -> list[str]:
    if net.prefixlen >= 31:
        return [str(a) for a in net][:count]
    first = int(net.network_address) + 1
    last = int(net.broadcast_address) - 1
    total = last - first + 1
    if total <= count:
        return [str(ipaddress.IPv4Address(i)) for i in range(first, last + 1)]
    picks: set[int] = set()
    while len(picks) < count:
        n = rng.randint(first, last)
        if n & 0xFF in (0, 255):
            continue
        picks.add(n)
    return [str(ipaddress.IPv4Address(i)) for i in sorted(picks)]


def load_seed_file(path: Path, sample_count: int, rng: random.Random) -> list[tuple[str, str]]:
    """讀取種子檔，支援每行 172.67.140.251#TW 或 104.28.16.0/24#HK 標註格式"""
    if not path.is_file():
        log.warning("種子檔案不存在：%s", path)
        return []
    items: list[tuple[str, str]] = []
    try:
        text = path.read_text(encoding="utf-8-sig")
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            tag_country = ""
            if "#" in line:
                parts = line.split("#", 1)
                line = parts[0].strip()
                tag_country = parts[1].strip().upper()

            if ":" in line and line.count(":") == 1:
                line = line.split(":", 1)[0].strip()

            for token in re.split(r"[\s,;]+", line):
                if not token:
                    continue
                try:
                    if "/" in token:
                        net = ipaddress.ip_network(token, strict=False)
                        if net.version == 4:
                            for sampled_ip in sample_network(net, sample_count, rng):
                                items.append((sampled_ip, tag_country))
                    else:
                        addr = ipaddress.ip_address(token)
                        if addr.version == 4:
                            items.append((str(addr), tag_country))
                except ValueError:
                    continue
    except Exception as exc:
        log.error("讀取種子檔失敗 %s：%s", path, exc)
    return items


def collect_local_seeds(cfg: dict[str, Any], root_dir: Path) -> list[dict[str, Any]]:
    if not cfg.get("enabled", True):
        return []
    sample_count = int(cfg.get("sample_per_cidr", 5))
    files = cfg.get("files", {})
    country_files = cfg.get("country_files", {})
    rng = random.Random(42)
    results: list[dict[str, Any]] = []

    for carrier, rel_path in files.items():
        full_path = root_dir / rel_path
        loaded = load_seed_file(full_path, sample_count, rng)
        for ip, country_tag in loaded:
            item = {
                "ip": ip,
                "carrier": carrier,
                "source": f"seeds_{carrier}",
            }
            if country_tag:
                item["source_country"] = country_tag
                item["country"] = country_tag.lower()
            results.append(item)

    for country, rel_path in country_files.items():
        full_path = root_dir / rel_path
        loaded = load_seed_file(full_path, sample_count, rng)
        for ip, country_tag in loaded:
            c_tag = country_tag or country.upper()
            results.append({
                "ip": ip,
                "carrier": None,
                "country": c_tag.lower(),
                "source_country": c_tag,
                "source": f"seeds_{country}",
            })

    global_files = cfg.get("global_files", {})
    global_sample_count = int(cfg.get("sample_per_cidr_global", 3))
    for name, rel_path in global_files.items():
        full_path = root_dir / rel_path
        loaded = load_seed_file(full_path, global_sample_count, rng)
        for ip, country_tag in loaded:
            results.append({
                "ip": ip,
                "carrier": None,
                "country": country_tag.lower() if country_tag else "cf",
                "source_country": country_tag or "CF",
                "source": f"seeds_{name}",
            })

    log.info("策略 D (本地種子) 成功展開 %d 筆記錄", len(results))
    return results


def load_enterprise_domains(domains_file: Path | str) -> list[dict[str, Any]]:
    """解析頂級企業優選域名 (如 store.ubi.com, openai.com) 動態注入高品質候選節點"""
    p = Path(domains_file)
    if not p.is_file():
        return []
    results: list[dict[str, Any]] = []
    lines = [
        line.strip() for line in p.read_text(encoding="utf-8", errors="ignore").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    hosts_to_resolve = []
    for line in lines:
        tag = ""
        if "#" in line:
            line, tag = line.split("#", 1)
            line, tag = line.strip(), tag.strip()
        host = line.split(":")[0].strip()
        if not host:
            continue
        if IPV4_RE.fullmatch(host):
            results.append({
                "ip": host,
                "carrier": "all",
                "source": "enterprise_entry",
                "source_country": "HK",
                "country": "hk",
            })
        else:
            hosts_to_resolve.append(host)

    def _resolve_one(h: str) -> tuple[str, list[str]]:
        try:
            infos = socket.getaddrinfo(h, 443, socket.AF_INET)
            ips = sorted(list(set(info[4][0] for info in infos)))
            return h, ips
        except Exception:
            return h, []

    if hosts_to_resolve:
        with ThreadPoolExecutor(max_workers=30) as ex:
            futures = [ex.submit(_resolve_one, h) for h in hosts_to_resolve]
            for fut in as_completed(futures):
                try:
                    h, ips = fut.result(timeout=2.0)
                    for ip in ips:
                        if IPV4_RE.fullmatch(ip):
                            results.append({
                                "ip": ip,
                                "carrier": "all",
                                "source": f"domain_{h}",
                                "source_country": "HK",
                                "country": "hk",
                            })
                except Exception:
                    pass

    log.info("策略 E (企業優選域名) 成功解析 %d 個動態 VIP IP", len(results))
    return results


def fetch_mainland_trinet_probes(timeout: float = 6.0) -> list[dict[str, Any]]:
    """從大陸境內實體三網探針 API 拉取當下實測低延遲 IP"""
    results: list[dict[str, Any]] = []
    # 1. WeTest 大陸三網即時優選 API (使用官方永久免費公鑰: o1zrmHAF, type=v4)
    try:
        resp = requests.get(
            "https://www.wetest.vip/api/cf2dns/get_cloudflare_ip?key=o1zrmHAF&type=v4",
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
        )
        if resp.status_code == 200:
            data = resp.json().get("info", {})
            if isinstance(data, dict):
                for carrier_key, code in [("CM", "cm"), ("CU", "cu"), ("CT", "ct")]:
                    for node in data.get(carrier_key, []):
                        if isinstance(node, dict):
                            ip = node.get("ip")
                            if ip and IPV4_RE.fullmatch(str(ip).strip()):
                                ip_str = str(ip).strip()
                                rtt = float(node.get("rtt_avg") or node.get("latency") or node.get("ping") or 60.0)
                                raw_spd = float(node.get("speed") or 50.0)
                                spd_mbps = (raw_spd * 8.0 / 1000.0) if raw_spd > 100 else (raw_spd * 8.0)
                                loss = float(node.get("loss_rate") or node.get("loss") or 0.0)
                                colo = str(node.get("colo", "")).strip().upper()
                                c_tag = get_country_by_colo(colo)
                                if code == "ct" and (rtt >= 100.0 or colo in ("SJC", "LAX", "SEA", "ORD", "DFW", "IAD", "ATL")):
                                    c_tag = "US"
                                results.append({
                                    "ip": ip_str,
                                    "source": f"wetest_{code}",
                                    "source_carrier": code,
                                    "carrier": code,
                                    "source_colo": colo,
                                    "source_country": c_tag or ("HK" if rtt < 80 else "US"),
                                    "country": (c_tag or "cf").lower(),
                                    "cn_latency_ms": rtt,
                                    f"cn_{code}_latency_ms": rtt,
                                    "cn_speed_mbps": round(spd_mbps, 2),
                                    f"cn_{code}_speed_mbps": round(spd_mbps, 2),
                                    "cn_loss_percent": loss,
                                    "download_speed": round(spd_mbps, 2),
                                    "loss_rate": loss,
                                })
    except Exception as exc:
        log.debug("fetch_mainland_trinet_probes wetest 略過: %s", exc)

    # 2. HostMonit (CloudFlareYes) 大陸三網即時探針
    try:
        resp = requests.post(
            "https://api.hostmonit.com/get_optimization_ip",
            json={"key": "iDetkOys"},
            headers={"User-Agent": USER_AGENT},
            timeout=4.0,
        )
        if resp.status_code == 200:
            info = resp.json().get("info", [])
            if isinstance(info, list):
                for node in info:
                    if isinstance(node, dict):
                        line = str(node.get("line", "")).upper()
                        code = "ct" if "CT" in line or "電信" in line or "电信" in line else ("cu" if "CU" in line or "聯通" in line or "联通" in line else "cm")
                        ip = node.get("ip")
                        if ip and IPV4_RE.fullmatch(str(ip).strip()):
                            ip_str = str(ip).strip()
                            lat = float(node.get("latency", 60))
                            spd = float(node.get("speed", 50)) * 8.0
                            loss = float(node.get("loss", 0.0))
                            colo = str(node.get("colo", "")).upper()
                            c_tag = get_country_by_colo(colo)
                            if code == "ct" and lat >= 100.0:
                                c_tag = "US"
                            results.append({
                                "ip": ip_str,
                                "source": f"hostmonit_{code}",
                                "source_carrier": code,
                                "carrier": code,
                                "source_colo": colo,
                                "source_country": c_tag or ("HK" if lat < 80 else "US"),
                                "country": (c_tag or "cf").lower(),
                                "cn_latency_ms": lat,
                                f"cn_{code}_latency_ms": lat,
                                "cn_speed_mbps": spd,
                                f"cn_{code}_speed_mbps": spd,
                                "cn_loss_percent": loss,
                                "download_speed": spd,
                                "loss_rate": loss,
                            })
    except Exception as exc:
        log.debug("fetch_mainland_trinet_probes hostmonit 略過: %s", exc)

    return results


# --------------------------------------------------------------------------- #
# 總調度收集器
# --------------------------------------------------------------------------- #
async def collect_all_candidates(
    config: dict[str, Any] | None = None,
    anchors: list[dict[str, Any]] | None = None,
    root_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """執行全策略收集並聚合為以 IP 為鍵的候選物件池，回傳候選資料字典清單。"""
    if root_dir is None:
        root_dir = Path(__file__).resolve().parent.parent
    if config is None:
        import yaml
        cfg_p = root_dir / "config" / "config.yaml"
        if cfg_p.is_file():
            config = yaml.safe_load(cfg_p.read_text(encoding="utf-8")) or {}
        else:
            config = {}
    if anchors is None:
        anchors_rel = config.get("collector", {}).get("ecs_doh", {}).get("anchors_file", "data/cn_anchors.json")
        anchors_p = root_dir / anchors_rel
        if anchors_p.is_file():
            try:
                a_data = json.loads(anchors_p.read_text(encoding="utf-8"))
                anchors = a_data.get("anchors", []) if isinstance(a_data, dict) else a_data
            except Exception:
                anchors = []
        else:
            anchors = []

    c_cfg = config.get("collector", {})
    candidates: dict[str, Candidate] = {}

    timeout = aiohttp.ClientTimeout(total=20.0)
    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT}) as session:
        cf_nets_task = fetch_cloudflare_ranges(session)
        ecs_task = collect_ecs_doh(session, c_cfg.get("ecs_doh", {}), anchors)
        mainland_task = collect_mainland_sources(session, c_cfg.get("mainland_sources", {}))
        country_task = collect_country_sources(session, c_cfg.get("country_sources", {}))

        cf_nets, ecs_rows, mainland_rows, country_rows = await asyncio.gather(
            cf_nets_task, ecs_task, mainland_task, country_task
        )

    # 執行境內即時三網探針 API 直連拉取 (第一引擎)
    loop = asyncio.get_running_loop()
    try:
        live_probe_rows = await loop.run_in_executor(None, fetch_mainland_trinet_probes, 6.0)
        log.info("第一引擎：境內實體三網探針 API 直連獲取 %d 筆記錄", len(live_probe_rows))
    except Exception as exc:
        log.debug("境內探針拉取異常：%s", exc)
        live_probe_rows = []

    seed_rows = collect_local_seeds(c_cfg.get("local_seeds", {}), root_dir)
    enterprise_rows = load_enterprise_domains(root_dir / "data" / "enterprise_domains.txt")

    all_raw_rows = ecs_rows + mainland_rows + country_rows + seed_rows + live_probe_rows + enterprise_rows
    log.info("五策略原始候選總筆數：%d 筆", len(all_raw_rows))

    for row in all_raw_rows:
        ip = row.get("ip")
        if not ip or not in_networks(ip, cf_nets):
            continue

        # 鐵律 2: 嚴格剔除繞美高延遲、高丟包黑名單網段
        if ip.startswith(("104.18.", "104.20.", "104.21.", "104.22.", "172.66.", "172.67.")):
            continue

        cand = candidates.get(ip)
        if cand is None:
            cand = Candidate(ip=ip)
            candidates[ip] = cand

        carrier = row.get("carrier")
        if carrier in {"ct", "cu", "cm", "edu"}:
            cand.carriers.add(carrier)
        elif carrier == "all":
            cand.carriers.update(["ct", "cu", "cm", "edu"])

        source_carrier = row.get("source_carrier")
        if source_carrier in {"ct", "cu", "cm"}:
            cand.source_carrier = source_carrier
            cand.carriers.add(source_carrier)

        country = row.get("country")
        if country:
            cand.countries.add(str(country).lower())

        source_country = row.get("source_country")
        if source_country and not cand.source_country:
            cand.source_country = str(source_country).upper()
            cand.countries.add(str(source_country).lower())

        source_colo = row.get("source_colo") or row.get("colo")
        if source_colo and not cand.source_colo:
            cand.source_colo = str(source_colo).upper()

        source = row.get("source")
        if source:
            cand.sources.add(source)

        if row.get("colo"):
            cand.source_hint["colo"] = row["colo"]
        if row.get("latency_ms"):
            cand.source_hint["latency_ms"] = float(row["latency_ms"])
        if row.get("cn_latency_ms") is not None:
            cand.cn_latency_ms = float(row["cn_latency_ms"])
            cand.source_hint["cn_latency_ms"] = cand.cn_latency_ms
        if row.get("cn_speed_mbps") is not None:
            cand.cn_speed_mbps = float(row["cn_speed_mbps"])
            cand.download_speed = max(cand.download_speed, cand.cn_speed_mbps)
            cand.source_hint["cn_speed_mbps"] = cand.cn_speed_mbps
        if row.get("cn_loss_percent") is not None:
            cand.cn_loss_percent = float(row["cn_loss_percent"])
            cand.loss_rate = cand.cn_loss_percent
            cand.source_hint["cn_loss_percent"] = cand.cn_loss_percent
        if row.get("loss_rate") is not None:
            cand.loss_rate = float(row["loss_rate"])
            cand.source_hint["loss_rate"] = float(row["loss_rate"])
        if row.get("download_speed") is not None:
            cand.download_speed = max(cand.download_speed, float(row["download_speed"]))
            cand.source_hint["download_speed"] = cand.download_speed
        if row.get("carrier") in {"ct", "cu", "cm"}:
            cand.source_hint["carrier"] = row["carrier"]

    results = [cand.to_dict() for cand in candidates.values()]
    log.info("Cloudflare 官方段過濾與去重後獨立 IP 數：%d 個", len(results))
    return results
