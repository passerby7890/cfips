"""CF-Global-QuadNet-Feed: 防牆阻斷檢測與國家標籤標註器 (v3.0.0)。

嚴格依據指令：
「解析出來的 IP 就直接看看他有沒有牆，如果沒牆就直接放到三網各自的清單內，並標註國家代碼」
- 透過 Cloudflare 官方邊緣憑證 (cdnjs.cloudflare.com:443 / :2053) 檢測 TCP + TLS 握手與 HTTP 響應
- 成功完成握手且未被 GFW 阻斷/重置 (Connection Reset) 者判定為健康活 IP
- 萃取邊緣機房代碼 (colo) 並解析國家標籤 (#TW, #HK, #JP, #SG 等)
"""

from __future__ import annotations

import asyncio
import ssl
from typing import Any, Dict, List, Optional

from src.region_mapper import (
    get_country_by_colo,
    is_cf_official_anycast,
    lookup_subnet_country,
    normalize_country_code,
    resolve_country_tag,
)


async def verify_single_ip_detailed(
    item: Dict[str, Any],
    semaphore: asyncio.Semaphore,
    sni: str = "",
    primary_port: int = 443,
    fallback_port: int = 2053,
) -> Dict[str, Any]:
    """測活單一 IP：檢查是否被 GFW 牆阻斷 (TCP/TLS)，詳細回傳狀態 (ALIVE / ERROR_1034 / TIMEOUT_OR_BLOCKED)"""
    ip = item["ip"]
    target_sni = sni or item.get("domain") or "cdnjs.cloudflare.com"
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    hit_1034 = False
    async with semaphore:
        for cur_sni in ([target_sni] if target_sni == "cdnjs.cloudflare.com" else [target_sni, "cdnjs.cloudflare.com"]):
            for port in [primary_port, fallback_port]:
                try:
                    conn = asyncio.open_connection(ip, port, ssl=ssl_ctx, server_hostname=cur_sni)
                    r, w = await asyncio.wait_for(conn, timeout=2.5)

                    req = (
                        f"GET /cdn-cgi/trace HTTP/1.1\r\n"
                        f"Host: {cur_sni}\r\n"
                        f"User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)\r\n"
                        f"Connection: close\r\n\r\n"
                    )
                    w.write(req.encode())
                    await w.drain()

                    raw_resp = await asyncio.wait_for(r.read(1500), timeout=2.0)
                    try:
                        w.close()
                        await w.wait_closed()
                    except Exception:
                        pass

                    text_resp = raw_resp.decode(errors="ignore")
                    if "1034" in text_resp or "error code: 1034" in text_resp:
                        hit_1034 = True
                        continue

                    if not text_resp or ("cloudflare" not in text_resp.lower() and "http/" not in text_resp.lower()):
                        continue

                    colo = ""
                    for line in text_resp.splitlines():
                        if line.startswith("colo="):
                            colo = line.split("=", 1)[1].strip().upper()
                            break
                        elif line.lower().startswith("cf-ray:"):
                            parts = line.strip().split("-")
                            if len(parts) >= 2:
                                colo = parts[-1].strip().upper()

                    c_carrier = str(item.get("carrier") or item.get("target_carrier") or "").lower()
                    country = resolve_country_tag(item, runner_trace_colo=colo, carrier=c_carrier)
                    if not country or country in ("CF", "UNKNOWN", "NONE"):
                        country = "SG" if ip.startswith("172.64.") and c_carrier == "ct" else "HK"

                    verified = dict(item)
                    verified["is_alive"] = True
                    verified["status"] = "ALIVE"
                    verified["colo"] = colo or "CF"
                    verified["country"] = country
                    return verified

                except Exception:
                    continue

    result = dict(item)
    result["is_alive"] = False
    result["status"] = "ERROR_1034" if hit_1034 else "TIMEOUT_OR_BLOCKED"
    return result


async def verify_single_ip(
    item: Dict[str, Any],
    semaphore: asyncio.Semaphore,
    sni: str = "",
    primary_port: int = 443,
    fallback_port: int = 2053,
) -> Optional[Dict[str, Any]]:
    """相容舊版單一 IP 驗證，僅回傳成功者"""
    res = await verify_single_ip_detailed(item, semaphore, sni=sni, primary_port=primary_port, fallback_port=fallback_port)
    return res if res.get("is_alive") else None


async def batch_verify_with_health_stats(
    candidates: List[Dict[str, Any]],
    concurrency: int = 35
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """並發批量測活所有候選 IP，同時回傳 (健康清單, 全體狀態清單)"""
    sem = asyncio.Semaphore(concurrency)
    tasks = [verify_single_ip_detailed(item, sem) for item in candidates]
    all_results = await asyncio.gather(*tasks)
    alive_list = [r for r in all_results if r.get("is_alive")]
    return alive_list, all_results


async def batch_verify_unblocked(candidates: List[Dict[str, Any]], concurrency: int = 35) -> List[Dict[str, Any]]:
    """並發批量測活所有候選 IP，篩除被牆與死亡 IP"""
    alive_list, _ = await batch_verify_with_health_stats(candidates, concurrency=concurrency)
    return alive_list


# 相容舊版名稱
batch_verify_101 = batch_verify_unblocked
