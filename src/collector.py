"""階段一：多維度情報收集器。

實作四大收集策略：
- 策略 A：基於北上廣四網錨點的 ECS (EDNS Client Subnet) Google DoH 偽裝解析
- 策略 B：大陸境內公開三網實測榜單抓取 (HostMonit, WeTest, uouin 等)
- 策略 C：分國家地區公開優選/反代來源聚合
- 策略 D：本地精選種子庫補充 (seeds_ct, seeds_cu, seeds_cm)
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp

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
    # 匹配 MB/s 或 MByte
    m_byte = re.search(r"(\d+(?:\.\d+)?)\s*(?:mb/s|mbyte/s|mbps|mb|兆)", text, re.I)
    if m_byte:
        val = float(m_byte.group(1))
        if "mb/s" in text.lower():
            return val * 8.0
        return val
    # 匹配 KB/s
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
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 cf-global-quadnet-feed/1.0"
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
    source_hint: dict[str, Any] = field(default_factory=dict)


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

    # 1. 異步確定每個錨點的實際可用 IP
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
                            if "visa.hk" in domain or domain.endswith(".hk"):
                                c_tag = "hk"
                            elif "visa.com.sg" in domain or domain.endswith(".sg"):
                                c_tag = "sg"
                            elif domain.endswith(".tw"):
                                c_tag = "tw"
                            elif domain.endswith(".jp"):
                                c_tag = "jp"

                            item = {
                                "ip": target_ip,
                                "carrier": carrier,
                                "source": f"ecs:{code}",
                                "domain": domain,
                            }
                            if c_tag:
                                item["country"] = c_tag
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
        # WeTest 中 '5 MB' 視為 40 Mbps (5 MB/s)
        if "5 mb" in row_text.lower() and (speed_mbps is None or speed_mbps < 40.0):
            speed_mbps = 40.0

        # 硬性門檻過濾：丟包率 <= 1.0%，下載頻寬 >= 20.0 Mbps
        if loss_rate is not None and loss_rate > MAX_LOSS_RATE:
            continue
        if speed_mbps is not None and speed_mbps < MIN_DOWNLOAD_SPEED:
            continue

        rows.append({
            "ip": m.group(1),
            "carrier": _detect_carrier(row_text),
            "colo": colo,
            "latency_ms": float(lat.group(1)) if lat else None,
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

        # 硬性門檻過濾：丟包率 <= 1.0%，下載頻寬 >= 20.0 Mbps
        if loss_rate > MAX_LOSS_RATE:
            continue
        if speed_mbps < MIN_DOWNLOAD_SPEED:
            continue

        rows.append({
            "ip": item["ip"],
            "carrier": HOSTMONIT_LINES.get(str(item.get("line", "")).upper()),
            "colo": colo if COLO_CELL_RE.match(colo) else None,
            "latency_ms": float(lat) if isinstance(lat, (int, float)) else None,
            "loss_rate": loss_rate,
            "download_speed": speed_mbps,
        })
    return rows


def parse_text(body: str) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for m in IPV4_RE.finditer(body):
        seen.setdefault(m.group(1), {
            "ip": m.group(1),
            "carrier": None,
            "colo": None,
            "latency_ms": None,
            "loss_rate": 0.0,
            "download_speed": 50.0,
        })
    return list(seen.values())


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

                    # 硬性門檻過濾：丟包率 <= 1.0%，下載頻寬 >= 20.0 Mbps
                    if loss_f is not None and loss_f > MAX_LOSS_RATE:
                        continue
                    if speed_f is not None and speed_f < MIN_DOWNLOAD_SPEED:
                        continue

                    rows.append({
                        "ip": ip,
                        "carrier": c,
                        "colo": colo if COLO_CELL_RE.match(colo) else None,
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
        if isinstance(rows, list):
            country_tag = src_def.get("country")
            for r in rows:
                r["source"] = src_def.get("name", "country_feed")
                if country_tag and not r.get("country"):
                    r["country"] = str(country_tag).lower()
                results.append(r)
    log.info("策略 C (分國來源) 成功獲取 %d 筆記錄", len(results))
    return results


# --------------------------------------------------------------------------- #
# 策略 D: 本地種子清單展開
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


def load_seed_file(path: Path, sample_count: int, rng: random.Random) -> list[str]:
    if not path.is_file():
        log.warning("種子檔案不存在：%s", path)
        return []
    ips: list[str] = []
    try:
        text = path.read_text(encoding="utf-8-sig")
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            for token in re.split(r"[\s,;]+", line):
                if not token:
                    continue
                try:
                    if "/" in token:
                        net = ipaddress.ip_network(token, strict=False)
                        if net.version == 4:
                            ips.extend(sample_network(net, sample_count, rng))
                    else:
                        addr = ipaddress.ip_address(token)
                        if addr.version == 4:
                            ips.append(str(addr))
                except ValueError:
                    continue
    except Exception as exc:
        log.error("讀取種子檔失敗 %s：%s", path, exc)
    return ips


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
        for ip in loaded:
            results.append({
                "ip": ip,
                "carrier": carrier,
                "source": f"seeds_{carrier}",
            })

    for country, rel_path in country_files.items():
        full_path = root_dir / rel_path
        loaded = load_seed_file(full_path, sample_count, rng)
        for ip in loaded:
            results.append({
                "ip": ip,
                "country": country,
                "source": f"seeds_{country}",
            })

    log.info("策略 D (本地種子) 成功展開 %d 筆記錄", len(results))
    return results


# --------------------------------------------------------------------------- #
# 總調度收集器
# --------------------------------------------------------------------------- #
async def collect_all_candidates(
    config: dict[str, Any],
    anchors: list[dict[str, Any]],
    root_dir: Path,
) -> dict[str, Candidate]:
    """執行全策略收集並聚合為以 IP 為鍵的候選物件池。"""
    c_cfg = config.get("collector", {})
    candidates: dict[str, Candidate] = {}

    timeout = aiohttp.ClientTimeout(total=20.0)
    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT}) as session:
        # 並行抓取 Cloudflare IP 段與三大網絡策略
        cf_nets_task = fetch_cloudflare_ranges(session)
        ecs_task = collect_ecs_doh(session, c_cfg.get("ecs_doh", {}), anchors)
        mainland_task = collect_mainland_sources(session, c_cfg.get("mainland_sources", {}))
        country_task = collect_country_sources(session, c_cfg.get("country_sources", {}))

        cf_nets, ecs_rows, mainland_rows, country_rows = await asyncio.gather(
            cf_nets_task, ecs_task, mainland_task, country_task
        )

    # 策略 D 為本機磁碟讀取
    seed_rows = collect_local_seeds(c_cfg.get("local_seeds", {}), root_dir)

    all_raw_rows = ecs_rows + mainland_rows + country_rows + seed_rows
    log.info("四策略原始候選總筆數：%d 筆", len(all_raw_rows))

    for row in all_raw_rows:
        ip = row.get("ip")
        if not ip or not in_networks(ip, cf_nets):
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

        country = row.get("country")
        if country:
            cand.countries.add(str(country).lower())

        source = row.get("source")
        if source:
            cand.sources.add(source)

        if row.get("colo"):
            cand.source_hint["colo"] = row["colo"]
        if row.get("latency_ms"):
            cand.source_hint["latency_ms"] = row["latency_ms"]
        if row.get("loss_rate") is not None:
            cand.loss_rate = float(row["loss_rate"])
        if row.get("download_speed") is not None:
            cand.download_speed = max(cand.download_speed, float(row["download_speed"]))

    log.info("Cloudflare 官方段過濾與去重後獨立 IP 數：%d 個", len(candidates))
    return candidates
