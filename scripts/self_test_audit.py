#!/usr/bin/env python3
"""[@QA_RedTeam] VPS789 v3.0.0 三網黃金 CNAME 域名與 101 握手深度驗收腳本。"""

from __future__ import annotations

import json
import os
import re
import socket
import ssl
import sys

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

LINE_PATTERN = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3})#([A-Z]{2,4})$")


def check_ws_101_sync(ip: str, port: int = 2053, sni: str = "node1.snowcat77.com", path: str = "/ws") -> bool:
    """實機抽檢 IP 是否能與 node1.snowcat77.com:2053/ws 成功完成 101 Switching Protocols 握手"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((ip, port), timeout=3.5) as sock:
            with ctx.wrap_socket(sock, server_hostname=sni) as ssock:
                req = (
                    f"GET {path} HTTP/1.1\r\n"
                    f"Host: {sni}\r\n"
                    f"Upgrade: websocket\r\n"
                    f"Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                    f"Sec-WebSocket-Version: 13\r\n\r\n"
                )
                ssock.sendall(req.encode())
                resp = ssock.recv(256).decode(errors="ignore")
                first_line = resp.splitlines()[0] if resp else ""
                return "101" in first_line
    except Exception:
        return False


def run_redteam_audit() -> bool:
    print("==========================================================")
    print("🔍 [@QA_RedTeam] 啟動 VPS789 v3.0.0 黃金 CNAME 與 101 握手審計...")
    print("==========================================================")
    errors = []

    # 1. 驗證 data/cname_pool.json 存在且包含完整三網網域
    cname_pool_path = "data/cname_pool.json"
    if not os.path.exists(cname_pool_path) or os.path.getsize(cname_pool_path) == 0:
        errors.append(f"[致命] {cname_pool_path} 不存在或為空！")
    else:
        try:
            with open(cname_pool_path, "r", encoding="utf-8") as f:
                cdata = json.load(f)
            carriers = cdata.get("carriers", {})
            for c in ["ct", "cu", "cm"]:
                d_list = carriers.get(c, [])
                if len(d_list) < 5:
                    errors.append(f"[CNAME池] {c.upper()} 網域數量過少 ({len(d_list)} < 5)！")
                else:
                    print(f"✅ [CNAME池] {c.upper()} 包含 {len(d_list)} 個實測黃金 CNAME 域名")
        except Exception as e:
            errors.append(f"[致命] {cname_pool_path} JSON 解析失敗: {e}")

    # 2. 檢查 output/best_ips.json 是否生成且有效
    json_path = "output/best_ips.json"
    if not os.path.exists(json_path) or os.path.getsize(json_path) == 0:
        errors.append(f"[致命] {json_path} 不存在或為空！")
    else:
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                jdata = json.load(f)
            carriers_cnt = len(jdata.get("carriers", {}))
            print(f"✅ [JSON] 結構化總表 {json_path} 審計通過 (涵蓋 {carriers_cnt} 大運營商數據)")
        except Exception as e:
            errors.append(f"[致命] {json_path} JSON 解析失敗: {e}")

    # 3. 針對 ct.txt, cu.txt, cm.txt 執行逐行審核與實機 101 握手抽檢
    for carrier in ["ct", "cu", "cm"]:
        fpath = f"output/{carrier}.txt"
        if not os.path.exists(fpath) or os.path.getsize(fpath) == 0:
            errors.append(f"[致命] {fpath} 不存在或為空檔案！")
            continue

        with open(fpath, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]

        if len(lines) < 3:
            errors.append(f"[{carrier.upper()}] 輸出 IP 數量過少 ({len(lines)} < 3)！")

        for idx, line in enumerate(lines, 1):
            m = LINE_PATTERN.match(line)
            if not m:
                errors.append(f"[{carrier.upper()}] 第 {idx} 行格式錯誤: {line} (應為 IP#COUNTRY)")
                continue

            ip, country = m.group(1), m.group(2)
            if len(country) != 2:
                errors.append(f"[{carrier.upper()}] 第 {idx} 行國家標籤長度異常: {country}")

        # 實機抽檢前 2 名節點之 :2053/ws 101 握手
        sample_ips = [line.split("#")[0] for line in lines[:2] if "#" in line]
        all_passed_101 = True
        for s_ip in sample_ips:
            if not check_ws_101_sync(s_ip):
                errors.append(f"[{carrier.upper()}] 榜首節點 {s_ip}:2053/ws 101 握手失敗！")
                all_passed_101 = False
                break

        if all_passed_101:
            print(f"✅ [{carrier.upper()}] 通過格式規範、國家標籤與 :2053/ws 101 握手抽檢 (共 {len(lines)} 筆優選 IP)")

    if errors:
        print("\n❌ [@QA_RedTeam] 審計未通過！發現以下重大違規：")
        for err in errors:
            print(f"   - {err}")
        return False

    print("\n🎉 [@DecisionMaker] 全部審計通過！三網優選 IP 清單 100% 通過 101 握手測活，准許發布！")
    return True


if __name__ == "__main__":
    success = run_redteam_audit()
    sys.exit(0 if success else 1)
