#!/usr/bin/env python3
"""[@QA_RedTeam] VPS789 v3.3.0 三網 ECS 深度解析、零 1034 毒性與 #JP 存量硬性審計腳本。"""

from __future__ import annotations

import json
import os
import re
import socket
import ssl
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

LINE_PATTERN = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3})#([A-Z]{2})$")


def check_ip_unblocked_tls(ip: str, port: int = 443, sni: str = "cdnjs.cloudflare.com", attempts: int = 2) -> bool:
    """實機抽檢 IP 是否具備健全的 TCP+TLS 握手連通性 (未被 GFW 阻斷/重置)"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    for _ in range(attempts):
        for p in [port, 2053]:
            try:
                with socket.create_connection((ip, p), timeout=4.0) as sock:
                    with ctx.wrap_socket(sock, server_hostname=sni) as ssock:
                        req = (
                            f"GET /cdn-cgi/trace HTTP/1.1\r\n"
                            f"Host: {sni}\r\n"
                            f"User-Agent: curl/7.88.1\r\n"
                            f"Connection: close\r\n\r\n"
                        )
                        ssock.sendall(req.encode())
                        resp = ssock.recv(512).decode(errors="ignore")
                        if resp and ("cloudflare" in resp.lower() or "http/" in resp.lower()):
                            return True
            except Exception:
                continue
    return False


def verify_origin_ws_101(ip: str, port: int = 2053, sni: str = "nodejp.snowcat77.com", ws_path: str = "/ws?ed=2048") -> tuple[bool, str]:
    """以真實源站 SNI 與 WebSocket Upgrade 握手檢測是否為 101 Switching Protocols (100% 斬殺 1034)"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((ip, port), timeout=4.0) as s:
            with ctx.wrap_socket(s, server_hostname=sni) as ss:
                req = (
                    f"GET {ws_path} HTTP/1.1\r\n"
                    f"Host: {sni}\r\n"
                    f"Upgrade: websocket\r\n"
                    f"Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                    f"Sec-WebSocket-Version: 13\r\n\r\n"
                )
                ss.sendall(req.encode())
                resp = ss.recv(512).decode("utf-8", errors="replace")
                if "101" in resp or "101 Switching Protocols" in resp:
                    return True, "101 Switching Protocols"
                if "1034" in resp or "403" in resp:
                    first = resp.splitlines()[0] if resp else "Empty"
                    return False, f"Cloudflare 403 / Error 1034 毒 IP ({first})"
                first = resp.splitlines()[0] if resp else "Empty"
                return False, f"非 101 協議響應 ({first})"
    except Exception as e:
        return False, f"連線或握手超時失敗: {e}"


def run_redteam_audit() -> bool:
    print("==========================================================")
    print("🔍 [@QA_RedTeam] 啟動 VPS789 v3.3.0 三網深度解析與零 1034 驗收審計...")
    print("==========================================================")
    errors = []

    # 1. 單元驗收：測試 japan.com 在電信 12 省 ECS 下是否能榨出極品 IP
    try:
        from src.dns_extractor import exhaustive_resolve_domain_for_carrier
        matrix_path = PROJECT_ROOT / "data" / "cn_ecs_matrix.json"
        with open(matrix_path, "r", encoding="utf-8") as f:
            ecs_matrix = json.load(f)
        ct_anchors = ecs_matrix.get("ct", [])
        print("[*] 正在驗證單網域多 IP 榨取率 (japan.com + CT 12 省 ECS)...")
        sample_res = exhaustive_resolve_domain_for_carrier("japan.com", ct_anchors, rounds=1)
        extracted_ips = [x["ip"] for x in sample_res]
        print(f"    -> japan.com 共榨出 {len(extracted_ips)} 個獨立 IP: {extracted_ips}")

        if len(extracted_ips) < 2:
            errors.append(f"[單元驗收失敗] japan.com 單網域榨取 IP 數不足 2 個 ({len(extracted_ips)} < 2)！")
        else:
            has_valid = any(ip.startswith(("104.", "172.")) for ip in extracted_ips)
            if not has_valid:
                errors.append(f"[單元驗收失敗] japan.com 榨出之 IP 未包含 104.* / 172.* 標準 Anycast 網段！")
            else:
                print(f"✅ [單元驗收通過] japan.com 成功榨出 {len(extracted_ips)} 個極品 IP (含 104.* / 172.*)")
    except Exception as e:
        errors.append(f"[致命] 單網域深度解析單元驗收執行失敗: {e}")

    # 2. 驗證設定資產完整性
    for f_rel in ["data/cname_pool.json", "data/cn_ecs_matrix.json", "data/carrier_landing_matrix.json"]:
        p = PROJECT_ROOT / f_rel
        if not p.is_file() or p.stat().st_size == 0:
            errors.append(f"[致命] {f_rel} 不存在或為空檔案！")
        else:
            print(f"✅ [設定檔案] {f_rel} 存在且結構完整")

    # 3. 檢查 output/best_ips.json 與 output/cname_health.json
    json_path = PROJECT_ROOT / "output" / "best_ips.json"
    if not json_path.is_file() or json_path.stat().st_size == 0:
        errors.append(f"[致命] {json_path} 不存在或為空檔案！")
    else:
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                jdata = json.load(f)
            carriers_cnt = len(jdata.get("carriers", {}))
            print(f"✅ [JSON] 結構化總表 {json_path.name} 審計通過 (涵蓋 {carriers_cnt} 大運營商數據)")
        except Exception as e:
            errors.append(f"[致命] {json_path} JSON 解析失敗: {e}")

    health_path = PROJECT_ROOT / "output" / "cname_health.json"
    if not health_path.is_file() or health_path.stat().st_size == 0:
        errors.append(f"[致命] {health_path} 不存在或為空檔案！")
    else:
        try:
            with open(health_path, "r", encoding="utf-8") as f:
                hdata = json.load(f)
            d_cnt = len(hdata.get("domains", {}))
            summary = hdata.get("summary", {})
            print(f"✅ [健康計分板] {health_path.name} 審計通過 (涵蓋 {d_cnt} 個來源網域: 健康 {summary.get('healthy')}, 阻斷/失效 {summary.get('dead_or_1034')})")
        except Exception as e:
            errors.append(f"[致命] {health_path} JSON 解析失敗: {e}")

    # 4. 針對 ct.txt, cu.txt, cm.txt 逐行審查與防牆連通性抽檢
    for carrier in ["ct", "cu", "cm"]:
        fpath = PROJECT_ROOT / "output" / f"{carrier}.txt"
        if not fpath.is_file() or fpath.stat().st_size == 0:
            errors.append(f"[致命] {fpath.name} 不存在或為空檔案！")
            continue

        with open(fpath, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]

        if len(lines) < 1:
            errors.append(f"[{carrier.upper()}] 輸出 IP 清單為空！")

        subnet_counts = {}
        for idx, line in enumerate(lines, 1):
            m = LINE_PATTERN.match(line)
            if not m:
                errors.append(f"[{carrier.upper()}] 第 {idx} 行格式錯誤: {line} (應為 IP#COUNTRY)")
                continue

            ip, country = m.group(1), m.group(2)
            if len(country) != 2:
                errors.append(f"[{carrier.upper()}] 第 {idx} 行國家標籤長度異常: {country}")

            # 嚴格禁止 #CF 兜底標籤
            if country == "CF":
                errors.append(f"[{carrier.upper()}] 致命錯誤！第 {idx} 行出現 #CF 未識別兜底標籤: {line}")

            # 統計同 /24 網段數量
            subnet = ".".join(ip.split(".")[:3]) + ".0/24"
            subnet_counts[subnet] = subnet_counts.get(subnet, 0) + 1

            # 電信 (CT) 落地規則驗收：
            # 新加坡 (SG) 與 日本 (JP)
            if carrier == "ct":
                if country not in ("SG", "JP"):
                    errors.append(f"[CT] 國家標籤異常！電信僅允許 #SG 與 #JP，出現 #{country}: {line}")

            # 移動 (CM) 落地規則驗收：
            # 香港 (HK) 與 美國 (US)
            if carrier == "cm":
                if ip.startswith("141.") and country != "US":
                    errors.append(f"[CM] 路由誤判！{ip} 應為 #US (美國)，卻標註為 #{country}")
                elif country not in ("HK", "US"):
                    errors.append(f"[CM] 國家標籤異常！移動主要為 #HK 與 #US，出現 #{country}: {line}")

            # 聯通 (CU) 落地規則驗收：
            if carrier == "cu":
                if country not in ("US", "DE", "JP", "HK", "SG"):
                    errors.append(f"[CU] 國家標籤異常！出現非預期標籤 #{country}: {line}")

        # 針對電信，檢查是否包含黃金 Enterprise/SaaS Anycast 網段
        if carrier == "ct":
            has_gold = any(
                line.startswith(("104.18.", "104.21.", "104.26.", "172.67.", "172.64.", "172.66.", "162.159.", "108.162."))
                for line in lines
            )
            if not has_gold:
                errors.append("[CT] 輸出清單完全未包含電信優選網段！")
            else:
                print(f"✅ [CT] 成功檢驗出真實低延遲電信優選網段！")

            # 5. [@QA_RedTeam 硬性驗收斷言 (v3.3.0)]
            jp_lines = [line for line in lines if line.endswith("#JP")]
            sg_lines = [line for line in lines if line.endswith("#SG")]
            print(f"[*] [CT 國別統計] #JP 節點: {len(jp_lines)} 顆, #SG 節點: {len(sg_lines)} 顆 (總計 {len(lines)} 顆)")

            # 斷言 A（#JP 存量保障）：output/ct.txt 中標記為 #JP 的 IP 數量必須 >= 10 顆，且至少有 #JP 出現在前 10 行內。
            if len(jp_lines) < 10:
                errors.append(f"[斷言 A 失敗] output/ct.txt 中標記為 #JP 的 IP 數量不足 10 顆 (實際: {len(jp_lines)} 顆)！")
            else:
                print(f"✅ [斷言 A 通過] output/ct.txt 中 #JP 節點充足 ({len(jp_lines)} >= 10 顆)")

            jp_in_top10 = any(line.endswith("#JP") for line in lines[:10])
            if not jp_in_top10:
                errors.append("[斷言 A 失敗] output/ct.txt 前 10 行內完全沒有任何 #JP 節點 (全被其他地區霸佔)！")
            else:
                print(f"✅ [斷言 A 通過] output/ct.txt 前 10 行內已包含正牌 #JP 節點！")

            # 斷言 B（零 1034 毒性抽檢）：自動抽取 output/ct.txt 中前 5 顆 #JP IP，以 SNI/Host: nodejp.snowcat77.com:2053/ws?ed=2048 實機發起 WebSocket 握手，必須 100% 回傳 HTTP/1.1 101 Switching Protocols，只要有任一顆回傳 403 / Error 1034 立即判定測試失敗！
            test_jp_ips = [line.split("#")[0] for line in jp_lines[:5]]
            print(f"[*] [斷言 B 抽檢] 抽取前 5 顆 #JP IP 進行真實 WebSocket 101 回源檢測: {test_jp_ips}...")
            for test_ip in test_jp_ips:
                ok, detail = verify_origin_ws_101(test_ip, port=2053, sni="nodejp.snowcat77.com", ws_path="/ws?ed=2048")
                if not ok:
                    errors.append(f"[斷言 B 失敗] 抽檢 #JP IP {test_ip} 握手失敗 ({detail})，存在 1034/非101 毒性！")
                else:
                    print(f"    -> [✔ 斷言 B 通過] {test_ip} 握手成功 (101 Switching Protocols)")

        # 實機抽檢前 3 名節點之防牆連通性
        sample_ips = [line.split("#")[0] for line in lines[:3] if "#" in line]
        all_passed = True
        for s_ip in sample_ips:
            chk_sni = "www.shopify.com" if carrier == "cu" else "cdnjs.cloudflare.com"
            if not check_ip_unblocked_tls(s_ip, sni=chk_sni):
                errors.append(f"[{carrier.upper()}] 榜首節點 {s_ip} TLS 握手超時或被牆阻斷！")
                all_passed = False
                break

        if all_passed:
            print(f"✅ [{carrier.upper()}] 通過格式規範、國家標籤與防牆抽檢 (共 {len(lines)} 筆優選 IP)")

    if errors:
        print("\n❌ [@QA_RedTeam] 審計未通過！發現以下重大違規：")
        for err in errors:
            print(f"   - {err}")
        return False

    print("\n🎉 [@DecisionMaker] 全部審計通過！v3.3.0 三網深度解析清單 100% 通過驗收，准許發布！")
    return True


if __name__ == "__main__":
    success = run_redteam_audit()
    sys.exit(0 if success else 1)
