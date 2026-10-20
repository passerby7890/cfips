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

# 嚴禁出現在三網輸出清單中的繞美高延遲黑名單前綴
BLACKLIST_PREFIXES = ("104.18.", "104.20.", "104.21.", "104.22.", "172.66.")
LINE_PATTERN = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3})#([A-Z]{2,4})$")


def check_ws_101_sync(ip: str, port: int = 2053, sni: str = "node1.snowcat77.com", path: str = "/ws") -> bool:
    """抽檢單一 IP 是否能真實與 V2bX 完成 101 WebSocket 握手"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((ip, port), timeout=3.0) as sock:
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
                return "101" in resp.splitlines()[0] if resp else False
    except Exception:
        return False


def run_redteam_audit() -> bool:
    print("==========================================================")
    print("🔍 [@QA_RedTeam] 啟動 CF 三網優選系統 5 大鐵律自動化審計...")
    print("==========================================================")
    errors = []

    # 鐵律 1: 檢查檔案存在性與格式規範 (<IP>#<COUNTRY>)
    for carrier in ["ct", "cu", "cm"]:
        fpath = f"output/{carrier}.txt"
        if not os.path.exists(fpath) or os.path.getsize(fpath) == 0:
            errors.append(f"[致命] {fpath} 不存在或為空檔案！")
            continue

        with open(fpath, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]

        if len(lines) < 10:
            errors.append(f"[{carrier.upper()}] 輸出 IP 數量過少 ({len(lines)} < 10)，情報源或黃金網段匹配異常！")

        subnet_counter = {}
        us_tag_count = 0

        for idx, line in enumerate(lines, 1):
            m = LINE_PATTERN.match(line)
            if not m:
                errors.append(f"[{carrier.upper()}] 第 {idx} 行格式錯誤: {line} (應為 IP#COUNTRY)")
                continue

            ip, country = m.group(1), m.group(2)

            # 鐵律 2: 嚴禁繞美高延遲黑名單網段混入
            if ip.startswith(BLACKLIST_PREFIXES):
                errors.append(f"[{carrier.upper()}] 發現黑名單高延遲網段混入: {ip} (第 {idx} 行)")

            # 鐵律 3: 同 /24 網段分散度檢查 (最多 3 個)
            prefix = ".".join(ip.split(".")[:3])
            subnet_counter[prefix] = subnet_counter.get(prefix, 0) + 1
            if subnet_counter[prefix] > 3:
                errors.append(f"[{carrier.upper()}] 網段 {prefix}.0/24 超出 3 個上限 ({subnet_counter[prefix]} 個)")

            if country == "US":
                us_tag_count += 1

        # 鐵律 4: 防止 GitHub 美國 Runner 洗標籤 (#US 比例不可超過 20%)
        if lines and (us_tag_count / len(lines)) > 0.20:
            errors.append(
                f"[{carrier.upper()}] #US 標籤比例過高 ({us_tag_count}/{len(lines)})，疑似再度發生美國 Runner 覆蓋亞太標籤！"
            )

        # 鐵律 5: 實機抽檢各網第 1 名與第 2 名 IP 的 :2053/ws 101 握手
        sample_ips = [line.split("#")[0] for line in lines[:2] if "#" in line]
        passed_101 = sum(1 for ip in sample_ips if check_ws_101_sync(ip))
        if passed_101 == 0 and sample_ips:
            errors.append(f"[{carrier.upper()}] 榜首 IP ({sample_ips}) 抽檢 :2053/ws 101 握手全部失敗！")
        else:
            print(f"✅ [{carrier.upper()}] 通過格式、黑名單攔截、/24 分散與 101 握手抽檢 (共 {len(lines)} 筆)")

    # 檢查 best_ips.json 國內延遲上限
    json_path = "output/best_ips.json"
    if os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            jdata = json.load(f)
        carriers_dict = jdata.get("carriers") or jdata.get("by_carrier") or {}
        for c_code, items in carriers_dict.items():
            top10 = items[:10]
            high_lat = [
                x["ip"] for x in top10
                if (float(x.get("cn_latency_ms") or x.get("est_cn_latency_ms") or 999.0)) > 95.0
            ]
            if high_lat:
                errors.append(f"[{c_code.upper()}] 前 10 名混入國內延遲 > 95ms 的節點: {high_lat}")

    if errors:
        print("\n❌ [@QA_RedTeam] 審計未通過！發現以下問題，請立即觸發專家自癒修復：")
        for err in errors:
            print(f"   - {err}")
        return False

    print("\n🎉 [@DecisionMaker] 5 大鐵律全數通過！清單品質符合 ITDog 亞太低延遲標準，准許發布！")
    return True


if __name__ == "__main__":
    success = run_redteam_audit()
    sys.exit(0 if success else 1)
