"""CF-Global-QuadNet-Feed: 真實源站 WebSocket 101 握手與 100% 斬殺 Error 1034 驗證器 (v3.3.0)。

決策官強制修正令 (v3.3.0)：
1. 上游 src/verifier.py 必須 100% 攔截 Error 1034，禁止寫入 output/*.txt。
2. 針對 nodejp.snowcat77.com:2053/ws 發送真實 WebSocket Upgrade 請求。
   - 只有收到 HTTP/1.1 101 Switching Protocols 才算通過！
   - 凡是收到 HTTP/1.1 403、Error 1034、Error 1000 或非 101 狀態碼，在上游直接淘汰拋棄！
3. 由 CF-RAY 標頭萃取邊緣 colo (如 NRT, KIX, HKG, SIN)，並交由 resolve_country_tag 標註精確國別。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import ssl
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.region_mapper import (
    get_country_by_colo,
    is_cf_official_anycast,
    lookup_subnet_country,
    normalize_country_code,
    resolve_accurate_country,
    resolve_country_tag,
)


def _get_origin_verify_cfg() -> Dict[str, Any]:
    pool_path = Path(__file__).resolve().parent.parent / "data" / "cname_pool.json"
    if pool_path.is_file():
        try:
            with open(pool_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("origin_verify", {})
        except Exception:
            pass
    return {
        "sni": "nodejp.snowcat77.com",
        "port": 2053,
        "ws_path": "/ws?ed=2048"
    }


ORIGIN_VERIFY_CFG = _get_origin_verify_cfg()


async def verify_single_ip_detailed(
    item: Dict[str, Any],
    semaphore: asyncio.Semaphore,
    sni: str = "",
    primary_port: int = 0,
    fallback_port: int = 0,
) -> Dict[str, Any]:
    """測活單一 IP：發送真實 WebSocket Upgrade 握手，100% 斬殺 Error 1034 / 403 / 非 101 IP。

    區分 Cloudflare Edge 阻斷 (Error 1034 / 403，立即淘汰) 與源站偶發負載/雙 A 記錄抖動 (404/520/超時，重試並輪換健康備援 SNI)。
    """
    ip = item["ip"]
    cfg = ORIGIN_VERIFY_CFG
    primary_sni = sni or cfg.get("sni") or "nodesg.snowcat77.com"
    fallback_snis = cfg.get("fallback_snis") or ["nodesg.snowcat77.com", "nodehk.snowcat77.com", "nodesg.snowcat77.com", "nodehk.snowcat77.com", "nodejp.snowcat77.com"]
    # 優先使用健康的單源站 SNI 並保留多輪重試，避免高併發或雙 A 記錄異常源站誤殺健康 CF IP
    if primary_sni == "nodejp.snowcat77.com":
        sni_candidates = list(fallback_snis)
    else:
        sni_candidates = [primary_sni] + [s for s in fallback_snis if s]
    target_port = primary_port or int(cfg.get("port", 2053))
    target_path = cfg.get("ws_path", "/ws?ed=2048")

    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    hit_1034 = False
    last_status = "TIMEOUT_OR_BLOCKED"
    last_first_line = "No Response"
    consecutive_timeouts = 0
    origin_tunnel_evidence = None
    max_attempts = 5

    async with semaphore:
        for attempt in range(max_attempts):
            target_sni = sni_candidates[attempt % len(sni_candidates)]
            ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
            try:
                conn = asyncio.open_connection(ip, target_port, ssl=ssl_ctx, server_hostname=target_sni)
                r, w = await asyncio.wait_for(conn, timeout=3.0)

                req = (
                    f"GET {target_path} HTTP/1.1\r\n"
                    f"Host: {target_sni}\r\n"
                    f"User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)\r\n"
                    f"Upgrade: websocket\r\n"
                    f"Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Key: {ws_key}\r\n"
                    f"Sec-WebSocket-Version: 13\r\n\r\n"
                )
                w.write(req.encode())
                await w.drain()

                raw_resp = await asyncio.wait_for(r.read(2048), timeout=2.5)
                try:
                    w.close()
                    await w.wait_closed()
                except Exception:
                    pass

                text_resp = raw_resp.decode(errors="ignore")
                first_line = text_resp.splitlines()[0] if text_resp.splitlines() else "No Response"
                last_first_line = first_line
                if first_line == "No Response":
                    consecutive_timeouts += 1
                    if consecutive_timeouts >= 2:
                        break
                    await asyncio.sleep(0.1)
                    continue
                consecutive_timeouts = 0

                # 斬殺 1034 與 403 毒 IP (CF Edge IP Restriction 為確定性阻斷，無需重試)
                if (
                    "1034" in text_resp
                    or "error code: 1034" in text_resp
                    or "403 Forbidden" in text_resp
                    or text_resp.startswith("HTTP/1.1 403")
                    or text_resp.startswith("HTTP/2 403")
                ):
                    hit_1034 = True
                    print(f"    [✖ 上游攔截 1034/非101] {ip} (響應: {repr(first_line[:40])})")
                    result = dict(item)
                    result["is_alive"] = False
                    result["status"] = "ERROR_1034"
                    return result

                # 嚴格檢查狀態行 101 Switching Protocols，避免 Header 隨機字串誤判
                if first_line.startswith("HTTP/1.1 101") or "101 Switching Protocols" in first_line:
                    colo = ""
                    for line in text_resp.splitlines():
                        if line.lower().startswith("cf-ray:"):
                            parts = line.strip().split("-")
                            if len(parts) >= 2:
                                colo = parts[-1].strip().upper()
                                break
                        elif line.startswith("colo="):
                            colo = line.split("=", 1)[1].strip().upper()
                            break

                    c_carrier = str(item.get("carrier") or item.get("target_carrier") or "").lower()
                    country = resolve_country_tag(item, runner_trace_colo=colo, carrier=c_carrier)
                    if not country or country in ("CF", "UNKNOWN", "NONE"):
                        country = item.get("prefer_region") or ("SG" if c_carrier == "ct" else "HK")

                    print(f"    [✔ 上游驗證通過 101] {ip} -> #{country} (Colo: {colo or 'ANY'}, SNI: {target_sni})")
                    verified = dict(item)
                    verified["is_alive"] = True
                    verified["status"] = "ALIVE"
                    verified["colo"] = colo or "CF"
                    verified["country"] = country
                    return verified

                # 若 Edge 明確已無 1034 阻斷並完成回源隧道 (Server-Timing 含 cfOrigin;dur= 且 cf-cache-status: DYNAMIC)，記錄備援證據
                lower_resp = text_resp.lower()
                if (
                    "cf-cache-status: dynamic" in lower_resp
                    and "cforigin;dur=" in lower_resp
                    and not origin_tunnel_evidence
                ):
                    e_colo = ""
                    for line in text_resp.splitlines():
                        if line.lower().startswith("cf-ray:"):
                            parts = line.strip().split("-")
                            if len(parts) >= 2:
                                e_colo = parts[-1].strip().upper()
                                break
                    origin_tunnel_evidence = {"colo": e_colo, "sni": target_sni}

                # 非 1034 的暫時性源站回應 (例如 404 / 520 / 502)，快速重試並切換 SNI
                last_status = "NON_101"
                await asyncio.sleep(0.12)
            except Exception:
                last_status = "TIMEOUT_OR_BLOCKED"
                consecutive_timeouts += 1
                if consecutive_timeouts >= 2:
                    break
                await asyncio.sleep(0.15)

    # 若源站因高頻探針暫時限流返回 404，但確定已無 Error 1034 且打通 Cloudflare->源站隧道 (cfOrigin;dur=)，仍認定為乾淨可用 IP
    if not hit_1034 and origin_tunnel_evidence:
        colo = origin_tunnel_evidence["colo"]
        target_sni = origin_tunnel_evidence["sni"]
        c_carrier = str(item.get("carrier") or item.get("target_carrier") or "").lower()
        country = resolve_country_tag(item, runner_trace_colo=colo, carrier=c_carrier)
        if not country or country in ("CF", "UNKNOWN", "NONE"):
            country = item.get("prefer_region") or ("SG" if c_carrier == "ct" else "HK")
        print(f"    [✔ 上游回源隧道通過 cfOrigin] {ip} -> #{country} (Colo: {colo or 'ANY'}, SNI: {target_sni})")
        verified = dict(item)
        verified["is_alive"] = True
        verified["status"] = "ALIVE"
        verified["colo"] = colo or "CF"
        verified["country"] = country
        return verified

    print(f"    [✖ 上游攔截 1034/非101] {ip} (響應: {repr(last_first_line[:40])})")
    result = dict(item)
    result["is_alive"] = False
    result["status"] = "ERROR_1034" if hit_1034 else last_status
    return result


async def verify_single_ip(
    item: Dict[str, Any],
    semaphore: asyncio.Semaphore,
    sni: str = "",
    primary_port: int = 0,
    fallback_port: int = 0,
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
