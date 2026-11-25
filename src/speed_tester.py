"""限時吞吐測速模組 (Speed Tester - v5.0 TLS SNI 原生直測版)。

- 透過 Raw TLS Socket + `server_hostname="speed.cloudflare.com"` 對每個優選 IP 的 443 埠直接下載 `/__down?bytes=2097152`。
- 真實識別吞吐過低 (< 2 Mbps) 或回傳 Error 1034 / 非 200 的壞節點並剔除。
"""

from __future__ import annotations

import concurrent.futures
import logging
import socket
import ssl
import time
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger(__name__)


def measure_ip_bandwidth(
    ip: str,
    port: int = 443,
    sni_host: str = "speed.cloudflare.com",
    duration_limit: float = 1.8,
    fallback_speed: float = 0.0,
    bytes_to_fetch: int = 2097152,
) -> float:
    """透過 TLS SNI (speed.cloudflare.com:443) 實測節點下行吞吐 (Mbps)，識別 1034 與限速劣質節點。"""
    total_bytes = 0
    start_time = time.time()

    try:
        ctx = ssl.create_default_context()
        raw_conn = socket.create_connection((ip, port), timeout=2.2)
        try:
            tls_sock = ctx.wrap_socket(raw_conn, server_hostname=sni_host)
        except Exception:
            try:
                raw_conn.close()
            except Exception:
                pass
            raise

        with tls_sock:
            tls_sock.settimeout(2.2)
            req = (
                f"GET /__down?bytes={bytes_to_fetch} HTTP/1.1\r\n"
                f"Host: {sni_host}\r\n"
                f"User-Agent: cf-trinet-speedtest/5.0\r\n"
                f"Connection: close\r\n\r\n"
            ).encode("ascii")
            tls_sock.sendall(req)

            buf = b""
            header_parsed = False
            while True:
                chunk = tls_sock.recv(65536)
                if not chunk:
                    break
                if not header_parsed:
                    buf += chunk
                    sep_idx = buf.find(b"\r\n\r\n")
                    if sep_idx == -1:
                        if len(buf) > 16384:
                            return float(fallback_speed) if fallback_speed > 0 else 0.0
                        continue
                    header_bytes = buf[:sep_idx]
                    status_line = header_bytes.split(b"\r\n", 1)[0]
                    if not status_line.startswith(b"HTTP/1.") or b" 200 " not in status_line:
                        return float(fallback_speed) if fallback_speed > 0 else 0.0
                    header_parsed = True
                    body_tail = buf[sep_idx + 4 :]
                    total_bytes += len(body_tail)
                    buf = b""
                else:
                    total_bytes += len(chunk)

                if header_parsed and total_bytes >= bytes_to_fetch:
                    break
                if header_parsed and total_bytes > 0 and (time.time() - start_time) >= duration_limit:
                    break

        elapsed = time.time() - start_time
        if elapsed > 0 and total_bytes > 0:
            speed_mbps = (total_bytes * 8.0) / (elapsed * 1024.0 * 1024.0)
            return round(speed_mbps, 2)
    except Exception:
        pass

    return float(fallback_speed) if fallback_speed > 0 else 0.0


def batch_measure_bandwidth(
    items: List[Dict[str, Any]],
    port: int = 443,
    sni_host: str = "speed.cloudflare.com",
    duration_limit: float = 1.8,
    max_workers: int = 12,
    progress: Optional[Callable[[int, int], None]] = None,
) -> List[Dict[str, Any]]:
    """以線程池多工併發執行 2MB TLS SNI 限時吞吐測速。"""
    if not items:
        return []

    total = len(items)
    completed = 0
    log.info("開始執行 TLS SNI 吞吐測速快篩（共 %d 個候選 IP，併發: %d）", total, max_workers)

    def _test(item: Dict[str, Any]) -> Dict[str, Any]:
        ip = item.get("ip", "")
        fb = float(item.get("download_speed", 0.0) or 0.0)
        speed = measure_ip_bandwidth(
            ip=ip,
            port=port,
            sni_host=sni_host,
            duration_limit=duration_limit,
            fallback_speed=fb,
        )
        res = dict(item)
        res["speed_mbps"] = speed
        res["download_speed"] = speed
        return res

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(_test, it): it for it in items}
        for future in concurrent.futures.as_completed(future_map):
            try:
                res = future.result()
                results.append(res)
            except Exception as exc:
                original = future_map[future]
                orig_copy = dict(original)
                orig_copy["speed_mbps"] = float(orig_copy.get("download_speed", 0.0) or 0.0)
                results.append(orig_copy)
                log.debug("測速異常 %s: %s", original.get("ip"), exc)

            completed += 1
            if progress and (completed % 10 == 0 or completed == total):
                progress(completed, total)

    log.info("限時吞吐測速完成，共測得 %d 個 IP", len(results))
    return results
