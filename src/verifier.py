"""CF-Global-QuadNet-Feed: 雙重測活（防牆阻斷 + :2053/ws 101 握手 + /cdn-cgi/trace 機房識別）(v3.0.0)。"""

from __future__ import annotations

import asyncio
import ssl
from typing import Any, Dict, List, Optional

from src.region_mapper import (
    get_country_by_colo,
    is_cf_official_anycast,
    lookup_subnet_country,
    normalize_country_code,
)


async def verify_single_ip(
    item: Dict[str, Any],
    semaphore: asyncio.Semaphore,
    sni: str = "node1.snowcat77.com",
    port: int = 2053,
    ws_path: str = "/ws",
) -> Optional[Dict[str, Any]]:
    ip = item["ip"]
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    async with semaphore:
        for attempt in range(2):
            try:
                # 1. 驗證 :2053/ws 真實 WebSocket 101 回源握手 (含防牆/端口阻斷檢測)
                conn = asyncio.open_connection(ip, port, ssl=ssl_ctx, server_hostname=sni)
                r, w = await asyncio.wait_for(conn, timeout=3.0)
                ws_req = (
                    f"GET {ws_path} HTTP/1.1\r\n"
                    f"Host: {sni}\r\n"
                    f"Upgrade: websocket\r\n"
                    f"Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                    f"Sec-WebSocket-Version: 13\r\n\r\n"
                )
                w.write(ws_req.encode())
                await w.drain()

                status_line = await asyncio.wait_for(r.readline(), timeout=3.0)
                if b"101" not in status_line:
                    try:
                        w.close()
                        await w.wait_closed()
                    except Exception:
                        pass
                    return None

                # 2. 讀取回應標頭，從 CF-RAY 萃取邊緣機房代碼 (例如 CF-RAY: a4db7d4139d08f07-TPE)
                colo = ""
                while True:
                    line = await asyncio.wait_for(r.readline(), timeout=1.5)
                    if not line or line == b"\r\n":
                        break
                    decoded_line = line.decode(errors="ignore")
                    if decoded_line.lower().startswith("cf-ray:"):
                        parts = decoded_line.strip().split("-")
                        if len(parts) >= 2:
                            colo = parts[-1].strip().upper()
                            break

                try:
                    w.close()
                    await w.wait_closed()
                except Exception:
                    pass

                # 若標頭未含 CF-RAY，兜底透過 /cdn-cgi/trace 獲取機房代碼
                if not colo:
                    try:
                        c_trace = asyncio.open_connection(ip, port, ssl=ssl_ctx, server_hostname=sni)
                        r_tr, w_tr = await asyncio.wait_for(c_trace, timeout=2.0)
                        t_req = (
                            f"GET /cdn-cgi/trace HTTP/1.1\r\n"
                            f"Host: {sni}\r\n"
                            f"User-Agent: Mozilla/5.0\r\n"
                            f"Connection: close\r\n\r\n"
                        )
                        w_tr.write(t_req.encode())
                        await w_tr.drain()
                        t_raw = await asyncio.wait_for(r_tr.read(1024), timeout=2.0)
                        try:
                            w_tr.close()
                            await w_tr.wait_closed()
                        except Exception:
                            pass
                        for t_line in t_raw.decode(errors="ignore").splitlines():
                            if t_line.startswith("colo="):
                                colo = t_line.split("=", 1)[1].strip().upper()
                                break
                    except Exception:
                        pass

                if not colo:
                    return None

                # 3. 決定國家簡寫標籤
                sub_tag = lookup_subnet_country(ip)
                norm_sub = normalize_country_code(sub_tag) if sub_tag else ""

                country = get_country_by_colo(colo)
                domain_hint = item.get("domain", "").lower()

                # 優先採用已知種子網段國家代碼
                if norm_sub and norm_sub != "US":
                    country = norm_sub
                # 防止美國 Runner 測官方 Anycast 時把亞太低延遲 CNAME 標成 US
                elif is_cf_official_anycast(ip) and country in ("US", ""):
                    if "ctsg" in domain_hint or "108.162." in ip:
                        country = "SG"
                    elif item.get("cn_latency_ms", 999) <= 95:
                        country = "HK"
                    else:
                        country = country or "US"

                verified = dict(item)
                verified["ws_handshake_101"] = True
                verified["colo"] = colo
                verified["country"] = country or "CF"
                return verified
            except Exception:
                if attempt == 0:
                    await asyncio.sleep(0.3)
                    continue
                return None


async def batch_verify_101(candidates: List[Dict[str, Any]], concurrency: int = 30) -> List[Dict[str, Any]]:
    sem = asyncio.Semaphore(concurrency)
    tasks = [verify_single_ip(item, sem) for item in candidates]
    results = await asyncio.gather(*tasks)
    return [r for r in results if r is not None]
    return [r for r in results if r is not None]
