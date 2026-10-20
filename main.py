#!/usr/bin/env python3
"""CF-Global-QuadNet-Feed 主程式入口 (v3.0 VPS789 重構版)。

遵循架構準則：
1. 完全遵循 VPS789 的三網專屬優選域名 (電信 CT / 移動 CM / 聯通 CU)。
2. 電信解析電信、移動解析移動、聯通解析聯通 (多通道 DoH + ECS 偽裝)。
3. 對解析出之 IP 進行防火牆連通性快篩 (TCP + TLS 握手，檢查是否有被牆)。
4. 未被牆且響應正常之節點，自動匹配大寫雙字元國別標籤 (#TW, #HK, #JP, #SG 等)。
5. 匯出至 output/ct.txt, cm.txt, cu.txt, domains.txt 及 best_ips.json。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import ipaddress
import json
import logging
import re
import shutil
import socket
import ssl
import sys
import time
from pathlib import Path
from typing import Any

import requests
import yaml

from src import __version__
from src.collector import fetch_cloudflare_ranges, in_networks
from src.region_mapper import (
    get_country_by_colo,
    init_subnet_country_map,
    lookup_subnet_country,
    normalize_country_code,
    resolve_country_tag,
)

PROJECT_ROOT = Path(__file__).resolve().parent

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 日誌格式設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")

IPV4_RE = re.compile(
    r"(?<![\d.])((?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?:\.(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3})(?![\d.])"
)

ECS_MAP = {
    "ct": ["101.80.0.1/24", "116.228.0.1/24", "218.1.0.1/24"],
    "cm": ["117.136.0.1/24", "221.130.33.1/24", "120.196.0.1/24"],
    "cu": ["123.125.0.1/24", "210.22.80.1/24", "112.96.0.1/24"],
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cf-global-quadnet-feed",
        description="VPS789 Cloudflare 三網優選 IP 自動化雲端生成系統",
    )
    parser.add_argument(
        "-c", "--config",
        default="config/config.yaml",
        help="設定檔路徑（預設 config/config.yaml）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="模擬執行模式（輸出報表至 output_dryrun）",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=30,
        help="測活併發數（預設 30）",
    )
    parser.add_argument(
        "-V", "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser.parse_args()


def load_config(cfg_path: Path) -> dict[str, Any]:
    if not cfg_path.is_file():
        return {}
    try:
        return yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def load_carrier_domains(file_path: Path) -> list[str]:
    """讀取運營商專屬優選域名清單"""
    if not file_path.is_file():
        return []
    domains = []
    for line in file_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # 移除端口與備註
        dom = line.split("#")[0].split(":")[0].strip()
        if dom and dom not in domains:
            domains.append(dom)
    return domains


def fetch_live_vps789_domains() -> dict[str, list[str]]:
    """自 VPS789 即時監控 API 取得合格域名"""
    res: dict[str, list[str]] = {"ct": [], "cm": [], "cu": []}
    try:
        r = requests.get("https://vps789.com/public/sum/cfIpTop20", timeout=4).json()
        items = r.get("data", {}).get("good", [])
        for item in items:
            raw_ip = str(item.get("ip", "")).strip()
            if not raw_ip or IPV4_RE.match(raw_ip):
                continue
            # CT 條件: 延遲 <= 80ms, 丟包 <= 2%
            if item.get("dxLatency", 999) <= 80 and item.get("dxPkgLostRate", 100) <= 2:
                res["ct"].append(raw_ip)
            # CM 條件: 延遲 <= 80ms, 丟包 <= 3%
            if item.get("ydLatency", 999) <= 80 and item.get("ydPkgLostRate", 100) <= 3:
                res["cm"].append(raw_ip)
            # CU 條件: 延遲 <= 150ms, 丟包 <= 3%
            if item.get("ltLatency", 999) <= 150 and item.get("ltPkgLostRate", 100) <= 3:
                res["cu"].append(raw_ip)
        log.info("[VPS789] 即時 API 動態聚合合格域名: 電信 %d, 移動 %d, 聯通 %d",
                 len(res["ct"]), len(res["cm"]), len(res["cu"]))
    except Exception as exc:
        log.debug("[VPS789] 即時 API 存取跳過: %s", exc)
    return res


def resolve_domain_for_carrier(domain: str, carrier: str) -> set[str]:
    """針對特定運營商解析域名之 IPv4"""
    ips: set[str] = set()

    # 1. 本地解析
    try:
        for info in socket.getaddrinfo(domain, 443, socket.AF_INET):
            ip = info[4][0]
            if IPV4_RE.match(ip):
                ips.add(ip)
    except Exception:
        pass

    # 2. AliDNS
    try:
        r = requests.get(f"https://dns.alidns.com/resolve?name={domain}&type=A", timeout=2.5).json()
        for a in r.get("Answer", []):
            if a.get("type") == 1 and IPV4_RE.match(a.get("data", "")):
                ips.add(a["data"])
    except Exception:
        pass

    # 3. DNSPod
    try:
        r = requests.get(
            f"https://doh.pub/dns-query?name={domain}&type=A",
            headers={"accept": "application/dns-json"},
            timeout=2.5
        ).json()
        for a in r.get("Answer", []):
            if a.get("type") == 1 and IPV4_RE.match(a.get("data", "")):
                ips.add(a["data"])
    except Exception:
        pass

    # 4. Google DoH 搭配該網專屬 ECS 偽裝
    for ecs in ECS_MAP.get(carrier, []):
        try:
            url = f"https://dns.google/resolve?name={domain}&type=A&edns_client_subnet={ecs}"
            r = requests.get(url, timeout=3.0).json()
            for a in r.get("Answer", []):
                if a.get("type") == 1 and IPV4_RE.match(a.get("data", "")):
                    ips.add(a["data"])
        except Exception:
            pass

    return ips


def check_unblocked_sync(ip: str, port: int = 443, timeout: float = 1.5) -> tuple[bool, float]:
    """檢測 IP 是否被防火牆阻斷 (TCP 連線 + TLS 握手)"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    t0 = time.time()
    try:
        s.connect((ip, port))
        with ctx.wrap_socket(s, server_hostname="speed.cloudflare.com") as ss:
            rtt = (time.time() - t0) * 1000.0
            return True, rtt
    except Exception:
        return False, 999.0
    finally:
        try:
            s.close()
        except Exception:
            pass


def get_ip_country(ip: str, carrier: str) -> str:
    """決定 IP 的二字元大寫國別代碼 (#TW, #HK, #JP 等)，防範海外 Runner 誤染 #US。"""
    # 1. 優先匹配已知的網段地圖
    sub_tag = lookup_subnet_country(ip)
    if sub_tag:
        norm = normalize_country_code(sub_tag)
        if norm and norm != "US":
            return norm

    # 2. 嘗試自 Cloudflare 邊緣 trace 獲取機房 (僅採信亞太/歐洲真實落地，海外 Runner 測得的 US 視為 Anycast 就近路由不採信)
    try:
        r = requests.get(f"http://{ip}/cdn-cgi/trace", headers={"Host": "speed.cloudflare.com"}, timeout=1.2)
        lines = dict(l.split("=", 1) for l in r.text.strip().split("\n") if "=" in l)
        colo = lines.get("colo", "").upper()
        c = get_country_by_colo(colo)
        if c and c in ("TW", "HK", "JP", "SG", "KR", "DE", "GB", "NL"):
            return c
    except Exception:
        pass

    # 3. 依據已知亞太網段前綴智慧判斷
    if ip.startswith(("162.159.38.", "162.159.39.", "162.159.130.", "162.159.193.", "188.114.97.")):
        return "JP"
    if ip.startswith((
        "162.159.44.", "162.159.192.", "108.162.198.", "108.162.236.",
        "104.18.33.", "104.18.43.", "104.19.153.", "172.64.144.",
        "172.64.153.", "172.64.53.", "172.66.44.", "172.66.47."
    )):
        return "HK"
    if ip.startswith((
        "162.159.152.", "162.159.153.", "172.64.229.", "172.64.52.", "172.64.145.",
        "104.16.151.", "104.18.42.", "104.17.101.", "104.17.185.",
        "104.21.87.", "104.21.76.", "172.67.202.", "188.114.96."
    )):
        return "TW"

    # 4. 運營商專屬預設亞太落地
    if carrier == "ct":
        return "HK"
    elif carrier == "cm":
        return "TW"
    else:
        return "TW"


def print_summary_table(title: str, headers: list[str], rows: list[list[str]]) -> None:
    print(f"\n=== {title} ===")
    if not rows:
        print("  (無資料)")
        return
    col_widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            w = sum(2 if ord(c) > 127 else 1 for c in str(cell))
            if w > col_widths[idx]:
                col_widths[idx] = w
    header_fmt = "  ".join(f"{{:<{w}}}" for w in col_widths)
    sep = "  ".join("-" * w for w in col_widths)
    print(header_fmt.format(*headers))
    print(sep)
    for row in rows:
        print(header_fmt.format(*row))


def main() -> int:
    args = parse_args()
    log.info("=" * 65)
    log.info("CF-Global-QuadNet-Feed v%s 啟動 (VPS789 三網直解直出架構)", __version__)
    log.info("指令規則：電信解電信、移動解移動、聯通解聯通，未牆節點直出清單")
    log.info("=" * 65)

    # 預載國家網段映射庫
    init_subnet_country_map(PROJECT_ROOT / "data")

    # 輸出目錄
    output_dir = PROJECT_ROOT / ("output_dryrun" if args.dry_run else "output")
    output_dir.mkdir(parents=True, exist_ok=True)

    # 讀取運營商域名清單
    carrier_domains = {
        "ct": load_carrier_domains(PROJECT_ROOT / "data" / "domains_ct.txt"),
        "cm": load_carrier_domains(PROJECT_ROOT / "data" / "domains_cm.txt"),
        "cu": load_carrier_domains(PROJECT_ROOT / "data" / "domains_cu.txt"),
    }

    # 動態補充 VPS789 實時監控合格域名
    live_domains = fetch_live_vps789_domains()
    for c in ["ct", "cm", "cu"]:
        for d in live_domains.get(c, []):
            if d not in carrier_domains[c]:
                carrier_domains[c].append(d)

    carriers_meta = [
        ("ct", "中國電信 (CT)", "domains_ct.txt"),
        ("cm", "中國移動 (CM)", "domains_cm.txt"),
        ("cu", "中國聯通 (CU)", "domains_cu.txt"),
    ]

    all_carrier_results: dict[str, list[dict[str, Any]]] = {}
    carrier_summary_rows = []

    for c_code, c_name, dom_file in carriers_meta:
        dom_list = carrier_domains.get(c_code, [])
        log.info("[*] [%s] 載入 %d 個 VPS789 專屬優選域名，開始多通道解析...", c_code.upper(), len(dom_list))

        # 1. 併發解析該網所有域名
        resolved_ips: set[str] = set()
        with concurrent.futures.ThreadPoolExecutor(max_workers=15) as ex:
            futs = {ex.submit(resolve_domain_for_carrier, d, c_code): d for d in dom_list}
            for f in concurrent.futures.as_completed(futs):
                d = futs[f]
                try:
                    ips = f.result()
                    resolved_ips.update(ips)
                    log.debug("[%s] 域名 %s 解析出 %d 個 IP", c_code.upper(), d, len(ips))
                except Exception as exc:
                    log.warning("[%s] 域名 %s 解析異常: %s", c_code.upper(), d, exc)

        log.info("[*] [%s] 共解析出 %d 個獨立 IP，開始防火牆測活快篩 (TCP+TLS 443)...", c_code.upper(), len(resolved_ips))

        # 2. 檢測防火牆連通性 (未被牆才採用)
        unblocked_nodes: list[dict[str, Any]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            futs = {ex.submit(check_unblocked_sync, ip): ip for ip in resolved_ips}
            for f in concurrent.futures.as_completed(futs):
                ip = futs[f]
                try:
                    ok, rtt = f.result()
                    if ok:
                        country = get_ip_country(ip, c_code)
                        unblocked_nodes.append({
                            "ip": ip,
                            "latency_ms": round(rtt, 1),
                            "country": country,
                            "tag": f"{ip}#{country}",
                            "status": "PASS_UNBLOCKED",
                        })
                except Exception:
                    pass

        # 3. 按延遲升序排序，並施加 /24 分散保護 (同網段最多 4 個，確保多樣性)
        unblocked_nodes.sort(key=lambda x: x["latency_ms"])
        final_nodes: list[dict[str, Any]] = []
        subnet_cnt: dict[str, int] = {}
        for node in unblocked_nodes:
            sub = ".".join(node["ip"].split(".")[:3])
            if subnet_cnt.get(sub, 0) < 4:
                final_nodes.append(node)
                subnet_cnt[sub] = subnet_cnt.get(sub, 0) + 1
            if len(final_nodes) >= 50:
                break

        # 若不足 50 個，不設網段限制補足剩餘節點
        if len(final_nodes) < 50:
            seen_ips = {n["ip"] for n in final_nodes}
            for node in unblocked_nodes:
                if node["ip"] not in seen_ips:
                    final_nodes.append(node)
                    seen_ips.add(node["ip"])
                if len(final_nodes) >= 50:
                    break

        all_carrier_results[c_code] = final_nodes
        top1 = final_nodes[0] if final_nodes else {}
        log.info("[+] [%s] 篩選完成：合格未被牆節點 %d 筆 (最佳節點: %s, 延遲: %sms)",
                 c_code.upper(), len(final_nodes), top1.get("tag", "N/A"), top1.get("latency_ms", "N/A"))

        # 寫出 output/<carrier>.txt
        out_txt = output_dir / f"{c_code}.txt"
        out_lines = [f"{n['tag']}\n" for n in final_nodes]
        out_txt.write_text("".join(out_lines), encoding="utf-8")
        log.info("[+] 清單已寫入 -> %s (%d 筆)", out_txt, len(final_nodes))

        carrier_summary_rows.append([
            c_name,
            str(len(dom_list)),
            str(len(resolved_ips)),
            str(len(final_nodes)),
            f"{top1.get('latency_ms', 0):.1f}ms",
            f"{out_txt.name}",
        ])

    # 輸出企業級 / VPS789 域名總清單至 output/domains.txt
    ent_src = PROJECT_ROOT / "data" / "enterprise_domains.txt"
    ent_dst = output_dir / "domains.txt"
    if ent_src.is_file():
        shutil.copy2(ent_src, ent_dst)
        log.info("[+] 優選域名總清單已同步 -> %s", ent_dst)

    # 輸出結構化 JSON 總表
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    summary_data = {
        "version": __version__,
        "strategy": "VPS789_TRI_NET_DIRECT",
        "updated_at": now.isoformat(timespec="seconds"),
        "timestamp": int(now.timestamp()),
        "by_carrier": all_carrier_results,
        "carriers": all_carrier_results,
    }
    json_path = output_dir / "best_ips.json"
    json_path.write_text(json.dumps(summary_data, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("[+] 結構化總表已輸出 -> %s", json_path)

    # 渲染終端摘要報表
    print_summary_table(
        "VPS789 三網優選 IP 與域名生成結果",
        ["運營商", "VPS789域名數", "解析IP總數", "未牆合格輸出", "最佳延遲", "輸出檔案"],
        carrier_summary_rows,
    )

    print("\n🎉 三網各自解析與防火牆連通性快篩全部順利完成！")
    return 0


if __name__ == "__main__":
    sys.exit(main())
