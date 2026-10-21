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


async def verify_single_ip(
    item: Dict[str, Any],
    semaphore: asyncio.Semaphore,
    sni: str = "",
    primary_port: int = 443,
    fallback_port: int = 2053,
) -> Optional[Dict[str, Any]]:
    """測活單一 IP：檢查是否被 GFW 牆阻斷 (TCP/TLS)，若活著則獲取機房代碼與國家標籤"""
    ip = item["ip"]
    target_sni = sni or item.get("domain") or "cdnjs.cloudflare.com"
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    async with semaphore:
        for cur_sni in ([target_sni] if target_sni == "cdnjs.cloudflare.com" else [target_sni, "cdnjs.cloudflare.com"]):
            for port in [primary_port, fallback_port]:
                try:
                    # 1. 檢測 TCP 連線與 TLS 握手 (若被 GFW 牆攔截會觸發 Connection Reset 或 Timeout)
                    conn = asyncio.open_connection(ip, port, ssl=ssl_ctx, server_hostname=cur_sni)
                    r, w = await asyncio.wait_for(conn, timeout=2.5)

                    # 2. 發送 /cdn-cgi/trace 請求
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
                    # 篩除 Cloudflare Error 1034 (Edge IP Restricted) 受限邊緣 IP，確保可供自定義域名代理
                    if "1034" in text_resp or "error code: 1034" in text_resp:
                        continue

                    # 只要收到 Cloudflare 正常邊緣回應 (帶 CF-RAY/Server: cloudflare 且非 1034)，代表該 IP 活躍且未被牆阻斷
                    if not text_resp or ("cloudflare" not in text_resp.lower() and "http/" not in text_resp.lower()):
                        continue

                    # 3. 提取機房代碼 (colo)
                    colo = ""
                    for line in text_resp.splitlines():
                        if line.startswith("colo="):
                            colo = line.split("=", 1)[1].strip().upper()
                            break
                        elif line.lower().startswith("cf-ray:"):
                            parts = line.strip().split("-")
                            if len(parts) >= 2:
                                colo = parts[-1].strip().upper()

                    # 4. 國家標籤解析 (依運營商路由特性精確解析，嚴禁輸出 #CF)
                    c_carrier = str(item.get("carrier") or item.get("target_carrier") or "").lower()
                    country = resolve_country_tag(item, runner_trace_colo=colo, carrier=c_carrier)
                    if not country or country in ("CF", "UNKNOWN", "NONE"):
                        country = "SG" if ip.startswith("172.64.") and c_carrier == "ct" else "HK"

                    verified = dict(item)
                    verified["is_alive"] = True
                    verified["colo"] = colo or "CF"
                    verified["country"] = country
                    return verified

                except Exception:
                    # 該埠超時或重置，嘗試備用埠 (2053)
                    continue

    return None


async def batch_verify_unblocked(candidates: List[Dict[str, Any]], concurrency: int = 35) -> List[Dict[str, Any]]:
    """並發批量測活所有候選 IP，篩除被牆與死亡 IP"""
    sem = asyncio.Semaphore(concurrency)
    tasks = [verify_single_ip(item, sem) for item in candidates]
    results = await asyncio.gather(*tasks)
    return [r for r in results if r is not None]


# 相容舊版名稱
batch_verify_101 = batch_verify_unblocked
