#!/usr/bin/env python3
"""[@QA_RedTeam] VPS789 v3.1.0 ITDog 同款三網 ECS 深度解析與防牆連通性深度驗收腳本。"""

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


def run_redteam_audit() -> bool:
    print("==========================================================")
    print("🔍 [@QA_RedTeam] 啟動 VPS789 v3.1.0 三網深度解析與防牆驗收審計...")
    print("==========================================================")
    errors = []

    # 1. 單元驗收：測試 cf.godns.cc 單網域在電信 12 省 ECS 下是否能榨出 >= 4 個極品 IP
    try:
        from src.dns_extractor import exhaustive_resolve_domain_for_carrier
        matrix_path = PROJECT_ROOT / "data" / "cn_ecs_matrix.json"
        with open(matrix_path, "r", encoding="utf-8") as f:
            ecs_matrix = json.load(f)
        ct_anchors = ecs_matrix.get("ct", [])
        print("[*] 正在驗證單網域多 IP 榨取率 (cf.godns.cc + CT 12 省 ECS)...")
        sample_res = exhaustive_resolve_domain_for_carrier("cf.godns.cc", ct_anchors, rounds=1)
        extracted_ips = [x["ip"] for x in sample_res]
        print(f"    -> cf.godns.cc 共榨出 {len(extracted_ips)} 個獨立 IP: {extracted_ips}")

        if len(extracted_ips) < 4:
            errors.append(f"[單元驗收失敗] cf.godns.cc 單網域榨取 IP 數不足 4 個 ({len(extracted_ips)} < 4)！")
        else:
            has_gold = any(ip.startswith(("162.159.", "108.162.", "172.64.52.", "172.64.53.")) for ip in extracted_ips)
            if not has_gold:
                errors.append(f"[單元驗收失敗] cf.godns.cc 榨出之 IP 未包含 162.159 / 108.162 / 172.64.52 電信極品網段！")
            else:
                print(f"✅ [單元驗收通過] cf.godns.cc 成功榨出 {len(extracted_ips)} 個極品 IP (含 162.159.* / 108.162.*)")
    except Exception as e:
        errors.append(f"[致命] 單網域深度解析單元驗收執行失敗: {e}")

    # 2. 驗證設定資產完整性
    for f_rel in ["data/cname_pool.json", "data/cn_ecs_matrix.json", "data/carrier_landing_matrix.json"]:
        p = PROJECT_ROOT / f_rel
        if not p.is_file() or p.stat().st_size == 0:
            errors.append(f"[致命] {f_rel} 不存在或為空檔案！")
        else:
            print(f"✅ [設定檔案] {f_rel} 存在且結構完整")

    # 3. 檢查 output/best_ips.json
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

    # 4. 針對 ct.txt, cu.txt, cm.txt 逐行審查與防牆連通性抽檢
    for carrier in ["ct", "cu", "cm"]:
        fpath = PROJECT_ROOT / "output" / f"{carrier}.txt"
        if not fpath.is_file() or fpath.stat().st_size == 0:
            errors.append(f"[致命] {fpath.name} 不存在或為空檔案！")
            continue

        with open(fpath, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]

        if len(lines) < 20:
            errors.append(f"[{carrier.upper()}] 輸出 IP 數量不足 20 筆 ({len(lines)} < 20)！")

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

            # 統計同 /24 網段數量，防範單一網段過載 (特色 5)
            subnet = ".".join(ip.split(".")[:3]) + ".0/24"
            subnet_counts[subnet] = subnet_counts.get(subnet, 0) + 1
            if subnet_counts[subnet] > 5:
                errors.append(f"[{carrier.upper()}] 特色 5 違規！同 /24 網段 {subnet} 收錄超過上限: {subnet_counts[subnet]} 筆")

            # 電信 (CT) 絕對禁止 104.21.* 與 172.67.* 繞美毒段
            if carrier == "ct" and ip.startswith(("104.21.", "172.67.")):
                errors.append(f"[CT] 致命違規！第 {idx} 行包含繞美毒段 IP: {ip}")

            # 電信 (CT) 172.64.144.* 物理路由經由新加坡 SIN 直連，必須標註為 #SG
            if carrier == "ct" and ip.startswith("172.64.144.") and country != "SG":
                errors.append(f"[CT] 路由誤判！172.64.144.* 應為 #SG (新加坡)，卻標註為 #{country}")

        # 針對電信，檢查是否包含黃金 Enterprise/SaaS Anycast 網段
        if carrier == "ct":
            has_gold = any(
                line.startswith(("162.159.", "108.162.", "172.64.52.", "172.64.53.", "172.64.32."))
                for line in lines
            )
            if not has_gold:
                errors.append("[CT] 輸出清單完全未包含 162.159.* / 108.162.* / 172.64.52.* 電信優選網段！")
            else:
                print(f"✅ [CT] 成功檢驗出 162.159.* / 108.162.* / 172.64.52.* 真實低延遲網段！")

        # 實機抽檢前 3 名節點之防牆連通性
        sample_ips = [line.split("#")[0] for line in lines[:3] if "#" in line]
        all_passed = True
        for s_ip in sample_ips:
            if not check_ip_unblocked_tls(s_ip):
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

    print("\n🎉 [@DecisionMaker] 全部審計通過！v3.1.0 三網深度解析清單 100% 通過驗收，准許發布！")
    return True


if __name__ == "__main__":
    success = run_redteam_audit()
    sys.exit(0 if success else 1)
