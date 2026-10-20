"""階段三：端到端真實回源 V2bX 驗證與國家機房識別 (v1.1.0 國家標籤保護版)。

- fast_precheck: 極短超時 (0.6s) TCP 端口前置快篩，秒殺過濾冷門鄰居假死 IP。
- TLS SNI 握手：連線至 <IP>:2053，SNI node1.snowcat77.com。
- /cdn-cgi/trace：解析機房 (colo) 與地區 (loc)。
- 國家標籤保護機制：
  若為 CF 官方 Anycast 網段，僅將海外 Runner 的 /cdn-cgi/trace 用於確認連線通暢，
  嚴禁覆蓋原生情報源/種子庫之亞太標籤 (HK/TW/JP/SG)。
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
from .region_mapper import colo_to_country, get_country_by_colo, resolve_country_tag

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) cf-global-quadnet-feed/2.1"


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
    country_code: str = ""
    loc: str = ""
    source_country: str = ""
    source_colo: str = ""
    runner_colo: str = ""
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
            "runner_colo": self.runner_colo,
            "source_colo": self.source_colo,
            "source_country": self.source_country,
            "country": self.country,
            "country_code": self.country_code,
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
                runner_colo = str(item.get("colo", "")).upper()
                c_tag = resolve_country_tag(item, runner_trace_colo=runner_colo)
                result["country_code"] = c_tag
                result["country"] = c_tag.lower()
                return result
        except Exception:
            pass

    return None


class OriginVerifier:
    def __init__(
        self,
        origin_cfg: dict[str, Any],
        concurrency: int = 40,
        precheck_concurrency: int = 100,
        handshake_concurrency: int = 50,
        precheck_timeout: float = 0.6,
    ):
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
        self.precheck_sem = asyncio.Semaphore(precheck_concurrency)
        self.handshake_sem = asyncio.Semaphore(handshake_concurrency or concurrency)
        self.sem = self.handshake_sem

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
        runner_colo = info.get("colo", "").upper()
        if not runner_colo:
            raise ValueError("trace 回應未包含 colo 欄位")

        res.runner_colo = runner_colo
        res.loc = info.get("loc", "")

        # 關鍵保護：保留境內實測原生 source_colo，不被海外 Runner 的 IAD/SJC 覆蓋
        res.colo = res.source_colo or runner_colo

        # 解析多層級國家標籤 (優先級: source_colo > source_country > Unicast反代 > Runner fallback)
        resolved_country = resolve_country_tag(
            {
                "ip": res.ip,
                "source_colo": res.source_colo,
                "source_country": res.source_country,
                "country": res.country,
                "countries": list(res.countries),
            },
            runner_trace_colo=runner_colo,
        )

        res.country = resolved_country.lower()
        res.country_code = resolved_country.upper()
        if resolved_country:
            res.countries.add(resolved_country.lower())

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

    async def _do_verify_handshake(self, cand: Candidate) -> VerifyResult:
        """執行 TLS 握手、trace 識別與 WebSocket 101 回源測活"""
        initial_c = cand.source_country.lower() if cand.source_country else None
        initial_countries = set(cand.countries)
        if cand.source_country:
            initial_countries.add(cand.source_country.lower())

        res = VerifyResult(
            ip=cand.ip,
            is_shifted_neighbor=cand.is_shifted_neighbor,
            carriers=set(cand.carriers),
            countries=initial_countries,
            sources=set(cand.sources),
            loss_rate=cand.loss_rate,
            loss_percent=cand.loss_rate,
            download_speed=cand.download_speed,
            speed_mbps=cand.download_speed,
            source_country=cand.source_country,
            source_colo=cand.source_colo,
            country=initial_c,
            country_code=cand.source_country.upper() if cand.source_country else "",
        )

        latencies: list[float] = []

        try:
            res.stage = "websocket_101"
            r, w, lat = await self._handshake(cand.ip)
            latencies.append(lat)
            try:
                key = "dGhlIHNhbXBsZSBub25jZQ=="
                req = (
                    f"GET {self.ws_path} HTTP/1.1\r\n"
                    f"Host: {self.host}\r\n"
                    f"Upgrade: websocket\r\n"
                    f"Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Key: {key}\r\n"
                    f"Sec-WebSocket-Version: 13\r\n"
                    f"User-Agent: {UA}\r\n\r\n"
                ).encode()
                w.write(req)
                await asyncio.wait_for(w.drain(), timeout=self.timeout)

                status_line = await asyncio.wait_for(r.readline(), timeout=self.timeout)
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

                # 自 101 回應標頭 CF-RAY 萃取 Colo (例如: cf-ray: a4d7ba9d5a8aa9c7-TPE)
                colo_found = ""
                for _ in range(30):
                    header_line = await asyncio.wait_for(r.readline(), timeout=self.timeout)
                    h_str = header_line.decode(errors="replace").strip()
                    if not h_str:
                        break
                    if h_str.lower().startswith("cf-ray:"):
                        ray_val = h_str.split(":", 1)[1].strip()
                        if "-" in ray_val:
                            colo_found = ray_val.split("-")[-1].strip().upper()

                colo = colo_found or "DEFAULT"
                res.runner_colo = colo
                if not res.colo or res.colo == "DEFAULT":
                    res.colo = res.source_colo or (colo if colo != "DEFAULT" else "")

                final_country = resolve_country_tag(res.to_dict(), runner_trace_colo=colo)
                res.country_code = final_country
                res.country = final_country.lower()

                res.ws_handshake_101 = True
                res.ok = True
                res.stage = "passed"
            finally:
                await _close(w)

            res.handshake_latencies = latencies
            res.latency_ms = lat
        except asyncio.TimeoutError:
            res.reason = f"超時 (> {self.timeout}s)"
        except Exception as exc:
            res.reason = f"{type(exc).__name__}: {exc}"

        return res

    async def verify_candidate(self, cand: Candidate) -> VerifyResult:
        """單一候選者完整驗證 (快篩 + 握手)"""
        if not await fast_precheck(cand.ip, self.port, timeout=self.precheck_timeout):
            return VerifyResult(
                ip=cand.ip,
                stage="fast_precheck",
                reason=f"TCP 端口 {self.port} 快速快篩未開放 (0.6s 超時)",
                is_shifted_neighbor=cand.is_shifted_neighbor,
                carriers=set(cand.carriers),
                countries=set(cand.countries),
                sources=set(cand.sources),
                source_country=cand.source_country,
                source_colo=cand.source_colo,
            )
        async with self.handshake_sem:
            return await self._do_verify_handshake(cand)

    async def verify_all(
        self,
        candidates: dict[str, Candidate],
        progress: Callable[[int, int, int], None] | None = None,
    ) -> dict[str, VerifyResult]:
        total = len(candidates)
        results: dict[str, VerifyResult] = {}

        # 漏斗第一層：高併發 TCP 端口快篩 (掃除未開端口與死 IP)
        log.info(
            "漏斗第一層：開始高併發 TCP 端口快篩（共 %d 個候選 IP，端口: %d，併發: %d，超時: %.1fs）",
            total, self.port, self.precheck_sem._value, self.precheck_timeout,
        )
        precheck_passed: list[Candidate] = []
        precheck_failed = 0

        async def _run_precheck(cand: Candidate) -> None:
            nonlocal precheck_failed
            async with self.precheck_sem:
                ok = await fast_precheck(cand.ip, self.port, timeout=self.precheck_timeout)
            if ok:
                precheck_passed.append(cand)
            else:
                precheck_failed += 1
                results[cand.ip] = VerifyResult(
                    ip=cand.ip,
                    stage="fast_precheck",
                    reason=f"TCP 端口 {self.port} 快速快篩未開放 (0.6s 超時)",
                    is_shifted_neighbor=cand.is_shifted_neighbor,
                    carriers=set(cand.carriers),
                    countries=set(cand.countries),
                    sources=set(cand.sources),
                    source_country=cand.source_country,
                    source_colo=cand.source_colo,
                )

        await asyncio.gather(*[_run_precheck(c) for c in candidates.values()])
        log.info(
            "漏斗第一層快篩完成：通過 %d 個（存活率 %.1f%%），淘汰死 IP %d 個",
            len(precheck_passed), (len(precheck_passed) / total * 100) if total else 0, precheck_failed
        )

        # 漏斗第二層：TLS + WebSocket 101 回源測活 (僅對端口開放節點進行)
        done = 0
        passed = 0
        log.info(
            "漏斗第二層：開始端到端 TLS + WebSocket 101 回源握手（共 %d 個存活 IP，併發: %d，超時: %.1fs）",
            len(precheck_passed), self.handshake_sem._value, self.timeout,
        )

        async def _run_handshake(cand: Candidate) -> None:
            nonlocal done, passed
            async with self.handshake_sem:
                r = await self._do_verify_handshake(cand)
            results[cand.ip] = r
            done += 1
            if r.ok:
                passed += 1
            if progress and (done % 10 == 0 or done == len(precheck_passed)):
                progress(done, len(precheck_passed), passed)

        await asyncio.gather(*[_run_handshake(c) for c in precheck_passed])
        log.info(
            "漏斗第二層握手完成：通過 101 握手: %d 個（通過率 %.1f%%）",
            passed, (passed / len(precheck_passed) * 100) if precheck_passed else 0
        )
        return results


async def batch_verify_101(
    candidates: list[dict[str, Any]] | dict[str, Candidate],
    origin_cfg: dict[str, Any] | None = None,
    concurrency: int = 50,
) -> list[dict[str, Any]]:
    """便捷封裝：執行漏斗 101 回源握手並回傳通過清單"""
    if origin_cfg is None:
        origin_cfg = {"host": "node1.snowcat77.com", "sni": "node1.snowcat77.com", "port": 2053, "ws_path": "/ws"}
    verifier = OriginVerifier(origin_cfg, handshake_concurrency=concurrency)
    if isinstance(candidates, list):
        cand_dict = {}
        for it in candidates:
            c = Candidate(
                ip=it["ip"],
                carriers=set(it.get("carriers", [])),
                countries=set(it.get("countries", [])),
                sources=set(it.get("sources", [])),
                source_country=it.get("source_country", ""),
                source_colo=it.get("source_colo", ""),
                is_shifted_neighbor=it.get("is_shifted_neighbor", False),
            )
            cand_dict[it["ip"]] = c
        candidates = cand_dict
    res = await verifier.verify_all(candidates)
    return [r.to_dict() for r in res.values() if r.ok and r.ws_handshake_101]
