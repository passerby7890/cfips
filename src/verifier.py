"""階段三：端到端真實回源 V2bX 驗證與國家機房識別 (v2.0 終極修訂版)。

- fast_precheck: 極短超時 (0.6s) TCP 端口前置快篩，秒殺過濾冷門鄰居假死 IP。
- TLS SNI 握手：連線至 <IP>:2053，SNI node1.snowcat77.com。
- /cdn-cgi/trace：解析機房 (colo) 與地區 (loc)，並映射為國家/地區代碼 (HK/TW/JP/SG/KR/US/EU)。
- WebSocket 101：發送 Upgrade: websocket 至 /ws，嚴格確認 101 Switching Protocols。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from .collector import Candidate
from .region_mapper import colo_to_country

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) cf-global-quadnet-feed/2.0"


async def fast_precheck(ip: str, port: int, timeout: float = 0.6) -> bool:
    """極短超時快篩：確認 TCP 端口開放，避免無效測速浪費時間"""
    try:
        conn = asyncio.open_connection(ip, port)
        reader, writer = await asyncio.wait_for(conn, timeout=timeout)
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False


@dataclass
class VerifyResult:
    ip: str
    ok: bool = False
    latency_ms: float = 0.0
    handshake_latencies: list[float] = field(default_factory=list)
    colo: str = ""
    country: str | None = None
    loc: str = ""
    ws_status: int | None = None
    ws_status_line: str = ""
    is_shifted_neighbor: bool = False
    carriers: set[str] = field(default_factory=set)
    countries: set[str] = field(default_factory=set)
    sources: set[str] = field(default_factory=set)
    loss_rate: float = 0.0
    download_speed: float = 50.0
    speed_mbps: float = 50.0
    loss_percent: float = 0.0
    ws_handshake_101: bool = False
    stage: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ip": self.ip,
            "ok": self.ok,
            "latency_ms": round(self.latency_ms, 2),
            "loss_rate": round(self.loss_rate, 2),
            "loss_percent": round(self.loss_rate, 2),
            "download_speed": round(self.download_speed, 2),
            "speed_mbps": round(self.speed_mbps, 2),
            "ws_handshake_101": self.ws_handshake_101,
            "colo": self.colo,
            "country": self.country,
            "countries": sorted(self.countries),
            "loc": self.loc,
            "ws_status": self.ws_status,
            "is_shifted_neighbor": self.is_shifted_neighbor,
            "carriers": sorted(self.carriers),
            "sources": sorted(self.sources),
        }


def build_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    # 強制協商 HTTP/1.1，避免 Cloudflare h2 協商導致 WebSocket Upgrade 失敗
    ctx.set_alpn_protocols(["http/1.1"])
    return ctx


async def _close(writer: asyncio.StreamWriter | None) -> None:
    if writer is None:
        return
    with contextlib.suppress(Exception):
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), timeout=1.0)


def parse_trace(body: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in body.decode("utf-8", errors="replace").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


async def verify_single_ip(item: Dict[str, Any], origin_cfg: Dict[str, Any], semaphore: asyncio.Semaphore) -> Optional[Dict[str, Any]]:
    """依規格書實作獨立快速驗證函式"""
    ip = item["ip"]
    port = int(origin_cfg.get("port", 2053))
    sni = str(origin_cfg.get("sni", "node1.snowcat77.com")).strip()
    ws_path = str(origin_cfg.get("ws_path", "/ws")).strip()
    precheck_timeout = float(origin_cfg.get("precheck_timeout_sec", 0.6))

    async with semaphore:
        # 1. 端口快篩
        if not await fast_precheck(ip, port, timeout=precheck_timeout):
            return None

        # 2. TLS + WebSocket 101 握手測試
        ssl_ctx = build_ssl_context()

        try:
            start_t = asyncio.get_event_loop().time()
            conn = asyncio.open_connection(ip, port, ssl=ssl_ctx, server_hostname=sni)
            reader, writer = await asyncio.wait_for(conn, timeout=2.5)
            latency = (asyncio.get_event_loop().time() - start_t) * 1000.0

            # 3. 發送 WebSocket 升級請求
            req = (
                f"GET {ws_path} HTTP/1.1\r\n"
                f"Host: {sni}\r\n"
                f"Upgrade: websocket\r\n"
                f"Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                f"Sec-WebSocket-Version: 13\r\n\r\n"
            )
            writer.write(req.encode())
            await writer.drain()

            res_line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            status_text = res_line.decode(errors="ignore")

            writer.close()
            await writer.wait_closed()

            # 4. 驗證是否回傳 101 Switching Protocols
            if "101" in status_text:
                result = dict(item)
                result["latency_ms"] = round(latency, 2)
                result["ws_handshake_101"] = True
                return result
        except Exception:
            pass

    return None


class OriginVerifier:
    def __init__(self, origin_cfg: dict[str, Any], concurrency: int = 40, precheck_timeout: float = 0.6):
        self.sni = str(origin_cfg.get("sni", "node1.snowcat77.com")).strip()
        self.host = str(origin_cfg.get("host", self.sni)).strip()
        self.port = int(origin_cfg.get("port", 2053))
        self.ws_path = str(origin_cfg.get("ws_path", "/ws")).strip()
        if not self.ws_path.startswith("/"):
            self.ws_path = "/" + self.ws_path
        self.timeout = float(origin_cfg.get("timeout_sec", 2.5))
        self.precheck_timeout = float(precheck_timeout)
        self.rounds = max(2, int(origin_cfg.get("handshake_rounds", 2)))
        self.ssl_ctx = build_ssl_context()
        self.sem = asyncio.Semaphore(concurrency)

    async def _handshake(self, ip: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, float]:
        """建立 TCP 連線並升級 TLS，回傳 (reader, writer, total_ms)。"""
        t0 = time.perf_counter()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, self.port),
            timeout=self.timeout,
        )
        t_tcp = time.perf_counter()

        await asyncio.wait_for(
            writer.start_tls(self.ssl_ctx, server_hostname=self.sni),
            timeout=self.timeout,
        )
        t_tls = time.perf_counter()

        total_ms = (t_tls - t0) * 1000.0
        return reader, writer, total_ms

    async def _check_trace(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, res: VerifyResult) -> None:
        req = (
            f"GET /cdn-cgi/trace HTTP/1.1\r\n"
            f"Host: {self.host}\r\n"
            f"User-Agent: {UA}\r\n"
            f"Accept: */*\r\n"
            f"Connection: close\r\n\r\n"
        ).encode()
        writer.write(req)
        await asyncio.wait_for(writer.drain(), timeout=self.timeout)

        status_line = await asyncio.wait_for(reader.readline(), timeout=self.timeout)
        if not status_line.startswith(b"HTTP/"):
            raise ValueError(f"無效 HTTP 回應: {status_line[:50]!r}")
        parts = status_line.decode(errors="replace").split()
        if len(parts) < 2 or parts[1] != "200":
            raise ValueError(f"/cdn-cgi/trace 回應非 200: {status_line.decode(errors='replace').strip()}")

        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=self.timeout)
            if not line or line in (b"\r\n", b"\n"):
                break

        body = await asyncio.wait_for(reader.read(4096), timeout=self.timeout)
        info = parse_trace(body)
        colo = info.get("colo", "").upper()
        if not colo:
            raise ValueError("trace 回應未包含 colo 欄位")

        res.colo = colo
        res.loc = info.get("loc", "")
        detected_c = colo_to_country(colo)
        res.country = detected_c
        if detected_c:
            res.countries.add(detected_c)

    async def _check_ws(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, res: VerifyResult) -> None:
        req = (
            f"GET {self.ws_path} HTTP/1.1\r\n"
            f"Host: {self.host}\r\n"
            f"User-Agent: {UA}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
        writer.write(req)
        await asyncio.wait_for(writer.drain(), timeout=self.timeout)

        status_line = await asyncio.wait_for(reader.readline(), timeout=self.timeout)
        line_str = status_line.decode(errors="replace").strip()
        res.ws_status_line = line_str

        parts = line_str.split()
        if len(parts) >= 2:
            try:
                res.ws_status = int(parts[1])
            except ValueError:
                pass

        if "101" not in line_str or "Switching Protocols" not in line_str:
            raise ValueError(f"WebSocket 回源握手失敗: {line_str}")
        res.ws_handshake_101 = True

    async def verify_candidate(self, cand: Candidate) -> VerifyResult:
        async with self.sem:
            res = VerifyResult(
                ip=cand.ip,
                is_shifted_neighbor=cand.is_shifted_neighbor,
                carriers=set(cand.carriers),
                countries=set(cand.countries),
                sources=set(cand.sources),
                loss_rate=cand.loss_rate,
                loss_percent=cand.loss_rate,
                download_speed=cand.download_speed,
                speed_mbps=cand.download_speed,
            )

            # 前置快篩：0.6s 極短超時 TCP 握手
            if not await fast_precheck(cand.ip, self.port, timeout=self.precheck_timeout):
                res.stage = "fast_precheck"
                res.reason = f"TCP 端口 {self.port} 快速快篩未開放 (0.6s 超時)"
                return res

            latencies: list[float] = []

            try:
                # 第 1 輪：握手 + trace 識別
                res.stage = "handshake_1"
                r1, w1, lat1 = await self._handshake(cand.ip)
                latencies.append(lat1)
                try:
                    res.stage = "trace"
                    await self._check_trace(r1, w1, res)
                finally:
                    await _close(w1)

                # 第 2 輪：握手 + WebSocket 101 回源測活
                res.stage = "handshake_2"
                r2, w2, lat2 = await self._handshake(cand.ip)
                latencies.append(lat2)
                try:
                    res.stage = "websocket_101"
                    await self._check_ws(r2, w2, res)
                finally:
                    await _close(w2)

                res.handshake_latencies = latencies
                res.latency_ms = sum(latencies) / len(latencies)
                res.ok = True
                res.ws_handshake_101 = True
                res.stage = "passed"
            except asyncio.TimeoutError:
                res.reason = f"超時 (> {self.timeout}s)"
            except Exception as exc:
                res.reason = f"{type(exc).__name__}: {exc}"

            return res

    async def verify_all(
        self,
        candidates: dict[str, Candidate],
        progress: Callable[[int, int, int], None] | None = None,
    ) -> dict[str, VerifyResult]:
        total = len(candidates)
        done = 0
        passed = 0
        results: dict[str, VerifyResult] = {}

        async def _run(cand: Candidate) -> None:
            nonlocal done, passed
            r = await self.verify_candidate(cand)
            results[cand.ip] = r
            done += 1
            if r.ok:
                passed += 1
            if progress and (done % 10 == 0 or done == total):
                progress(done, total, passed)

        log.info(
            "開始端到端回源驗證 %d 個候選 IP（SNI: %s, 端口: %d, WS: %s, 併發: %d, 快篩: %.1fs）",
            total, self.sni, self.port, self.ws_path, self.sem._value, self.precheck_timeout,
        )
        tasks = [_run(c) for c in candidates.values()]
        await asyncio.gather(*tasks)

        log.info(
            "回源驗證完成：%d 個 IP 測試，通過 101 握手: %d 個（通過率 %.1f%%）",
            total, passed, (passed / total * 100) if total else 0
        )
        return results
