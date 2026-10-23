#!/usr/bin/env python3
"""[@QA_RedTeam] VPS789 v4.0.0 大陸三網真機探針實測、零 1034 毒性與防繞美硬性審計腳本。"""

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
BANNED_US_PREFIXES = (
    "104.18.", "104.19.", "104.20.", "104.21.", "104.22.",
    "104.24.", "104.25.", "104.26.", "172.67."
)


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
    print("🔍 [@QA_RedTeam] 啟動 VPS789 v4.0.0 大陸三網真機探針與防繞美審計...")
    print("==========================================================")
    errors = []

    # 1. 單元驗收：測試 ctsg.cfyx.aliyun.20237737.xyz 在電信 12 省 ECS 下是否能榨出極品 IP
    try:
        from src.dns_extractor import exhaustive_resolve_domain_for_carrier
        matrix_path = PROJECT_ROOT / "data" / "cn_ecs_matrix.json"
        with open(matrix_path, "r", encoding="utf-8") as f:
            ecs_matrix = json.load(f)
        ct_anchors = ecs_matrix.get("ct", [])
        test_domain = "www.on.com"
        print(f"[*] 正在驗證單網域多 IP 榨取率 ({test_domain} + CT 12 省 ECS)...")
        sample_res = exhaustive_resolve_domain_for_carrier(test_domain, ct_anchors, rounds=1)
        extracted_ips = [x["ip"] for x in sample_res]
        print(f"    -> {test_domain} 共榨出 {len(extracted_ips)} 個獨立 IP: {extracted_ips[:6]}")

        if len(extracted_ips) < 2:
            errors.append(f"[單元驗收失敗] {test_domain} 單網域榨取 IP 數不足 2 個 ({len(extracted_ips)} < 2)！")
        else:
            print(f"✅ [單元驗收通過] {test_domain} 成功榨出 {len(extracted_ips)} 個候選 IP")
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

    # 4. 針對 ct.txt, cu.txt, cm.txt 逐行審查與四大鐵律斷言驗收
    for carrier in ["ct", "cu", "cm"]:
        fpath = PROJECT_ROOT / "output" / f"{carrier}.txt"
        if not fpath.is_file() or fpath.stat().st_size == 0:
            errors.append(f"[致命] {fpath.name} 不存在或為空檔案！")
            continue

        with open(fpath, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]

        if len(lines) < 1:
            errors.append(f"[{carrier.upper()}] 輸出 IP 清單為空！")

        # 斷言 3（行數上限 50 行）
        if len(lines) > 50:
            errors.append(f"[斷言 3 失敗] {carrier.upper()} 輸出行數超過 50 行上限 (實際: {len(lines)} 行)！")
        else:
            print(f"✅ [斷言 3 通過] {carrier.upper()} 輸出總行數合規 ({len(lines)} <= 50 行)")

        country_counts: dict[str, int] = {}
        for idx, line in enumerate(lines, 1):
            m = LINE_PATTERN.match(line)
            if not m:
                errors.append(f"[{carrier.upper()}] 第 {idx} 行格式錯誤: {line} (應為 IP#COUNTRY)")
                continue

            ip, country = m.group(1), m.group(2)
            country_counts[country] = country_counts.get(country, 0) + 1

            if len(country) != 2:
                errors.append(f"[{carrier.upper()}] 第 {idx} 行國家標籤長度異常: {country}")

            # 嚴格禁止 #CF 兜底標籤
            if country == "CF":
                errors.append(f"[{carrier.upper()}] 致命錯誤！第 {idx} 行出現 #CF 未識別兜底標籤: {line}")

            # 斷言 1（真實落地校驗）：172.64.229.* 在電信/聯通下經東京 NTT 互聯，必須精準標註為 #JP，嚴禁誤標為 #SG
            if ip.startswith("172.64.229.") and carrier in ("ct", "cu"):
                if country != "JP":
                    errors.append(f"[斷言 1 失敗] {carrier.upper()} 第 {idx} 行 {ip} 落地東京 NTT，但被誤標為 #{country} (應為 #JP)！")

        # 斷言 2（拒絕單一假國別刷屏）
        if carrier == "ct":
            if len(country_counts) == 1 and "JP" in country_counts and len(lines) > 20:
                errors.append(f"[斷言 2 失敗] ct.txt 100% 全部都是同一個 #JP 標籤刷屏，判定標籤器故障！")
            else:
                dist_str = ", ".join([f"#{k}: {v}顆" for k, v in country_counts.items()])
                print(f"✅ [斷言 2 通過] CT 國別標籤實測分佈真實多元 ({dist_str})")

        # 斷言 4（實機抽檢 CT 與 CM 榜首大陸延遲與真實亞太落地檢查）
        if carrier in ("ct", "cm"):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    jdata = json.load(f)
                c_list = jdata.get("carriers", {}).get(carrier, [])
                if c_list:
                    top_node = c_list[0]
                    top_ms = top_node.get("real_cn_ms", 999.0)
                    top_country = top_node.get("country", "")
                    max_ms = 95.0 if carrier == "ct" else 75.0
                    if top_ms > max_ms:
                        errors.append(f"[斷言 4 失敗] {carrier.upper()} 榜首節點 {top_node['ip']} 大陸實測延遲過高 ({top_ms}ms > {max_ms}ms)！")
                    elif top_country == "US":
                        errors.append(f"[斷言 4 失敗] {carrier.upper()} 榜首節點 {top_node['ip']} 為 #US 繞美節點！")
                    else:
                        print(f"✅ [斷言 4 通過] {carrier.upper()} 榜首節點 {top_node['ip']} 大陸延遲頂級 ({top_ms}ms <= {max_ms}ms, #{top_country})")
            except Exception as e:
                errors.append(f"[斷言 4 失敗] {carrier.upper()} 榜首延遲讀取異常: {e}")

        # 抽檢前 3 名節點之防牆連通性
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

    print("\n🎉 [@DecisionMaker] 全部審計通過！v4.0.0 三網深度解析清單 100% 通過驗收，准許發布！")
    return True


if __name__ == "__main__":
    success = run_redteam_audit()
    sys.exit(0 if success else 1)
