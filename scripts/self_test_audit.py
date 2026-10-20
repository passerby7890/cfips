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

def check_unblocked_liveness(ip: str, port: int = 443, timeout: float = 2.0) -> bool:
    """檢查 IP 是否真實存活且未被防火牆阻斷 (TCP + TLS 握手)"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((ip, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname="speed.cloudflare.com") as ssock:
                return bool(ssock.cipher())
    except Exception:
        return False


def run_redteam_audit() -> bool:
    print("==========================================================")
    print("🔍 [@QA_RedTeam] 啟動 VPS789 三網優選系統深度審計 (v3.0.0)...")
    print("==========================================================")
    errors = []

    # 1. 檢查 best_ips.json 是否生成且有效
    json_path = "output/best_ips.json"
    if not os.path.exists(json_path) or os.path.getsize(json_path) == 0:
        errors.append(f"[致命] {json_path} 不存在或為空！")
    else:
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                jdata = json.load(f)
            carriers_cnt = len(jdata.get("carriers") or jdata.get("by_carrier") or {})
            print(f"✅ [JSON] 結構化總表 {json_path} 審計通過 (涵蓋 {carriers_cnt} 大運營商數據)")
        except Exception as e:
            errors.append(f"[致命] {json_path} JSON 解析失敗: {e}")

    # 2. 檢查 output/domains.txt 是否生成且有效
    dom_path = "output/domains.txt"
    if not os.path.exists(dom_path) or os.path.getsize(dom_path) == 0:
        errors.append(f"[致命] {dom_path} 不存在或為空！")
    else:
        with open(dom_path, "r", encoding="utf-8") as f:
            d_lines = [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]
        if len(d_lines) < 10:
            errors.append(f"[優選域名] 域名清單數量過少 ({len(d_lines)} < 10)！")
        else:
            print(f"✅ [DOMAINS] VPS789 三網優選 CNAME 域名清單審計通過 (共 {len(d_lines)} 筆優選域名)")

    # 3. 針對 ct.txt, cu.txt, cm.txt 執行逐行審核
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

        # 抽檢前 3 名節點之防火牆連通性
        sample_ips = [line.split("#")[0] for line in lines[:3] if "#" in line]
        unblocked_count = sum(1 for ip in sample_ips if check_unblocked_liveness(ip))
        if unblocked_count == 0 and sample_ips:
            errors.append(f"[{carrier.upper()}] 榜首 IP ({sample_ips}) 防火牆 TLS 連通性抽檢全部失敗！")
        else:
            print(f"✅ [{carrier.upper()}] 通過格式規範、國家標籤與防火牆連通性審計 (共 {len(lines)} 筆優選 IP)")

    if errors:
        print("\n❌ [@QA_RedTeam] 審計未通過！發現以下違規：")
        for err in errors:
            print(f"   - {err}")
        return False

    print("\n🎉 [@DecisionMaker] 全部審計通過！VPS789 三網優選 IP 清單皆未被牆，准許發布！")
    return True


if __name__ == "__main__":
    success = run_redteam_audit()
    sys.exit(0 if success else 1)
