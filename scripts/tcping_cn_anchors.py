#!/usr/bin/env python3
"""北上廣四網 12 基準錨點 TCPing (80/443) 探針工具。

供外部機器 / VPS 定期探測自身連線至大陸北上廣電信、聯通、移動、教育網骨幹的 RTT 與雙向通暢性。
支援解析動態維護域名 (*.818198.xyz) 與靜態 IP 回退。
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("tcping")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="北上廣四網基準錨點 TCPing 探測工具")
    parser.add_argument(
        "-f", "--file",
        default=str(PROJECT_ROOT / "data" / "cn_anchors.json"),
        help="錨點資料庫 JSON 檔案路徑",
    )
    parser.add_argument(
        "-t", "--timeout",
        type=float,
        default=2.0,
        help="單次 TCP 握手超時時間 (秒)",
    )
    parser.add_argument(
        "-p", "--ports",
        nargs="+",
        type=int,
        default=[80, 443],
        help="探測的目標端口 (預設 80 443)",
    )
    return parser.parse_args()


def resolve_ip(anchor: dict) -> str:
    domain = anchor.get("domain")
    if domain:
        try:
            return socket.gethostbyname(domain)
        except Exception:
            pass
    return anchor.get("ipv4", "")


def tcp_ping(ip: str, port: int, timeout: float) -> tuple[bool, float, str]:
    t0 = time.perf_counter()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((ip, port))
        rtt = (time.perf_counter() - t0) * 1000.0
        return True, rtt, ""
    except socket.timeout:
        return False, 0.0, "timeout"
    except Exception as exc:
        return False, 0.0, str(exc)
    finally:
        sock.close()


def main() -> None:
    args = parse_args()
    data_path = Path(args.file)
    if not data_path.is_file():
        sys.exit(f"[-] 找不到錨點檔案：{data_path}")

    anchors = json.loads(data_path.read_text(encoding="utf-8")).get("anchors", [])
    log.info("=" * 70)
    log.info("北上廣四網基準錨點 TCPing 探測開始 (共 %d 個錨點, 端口: %s)", len(anchors), args.ports)
    log.info("=" * 70)

    rows = []
    for a in anchors:
        resolved_ip = resolve_ip(a)
        port_results = []
        for port in args.ports:
            ok, rtt, err = tcp_ping(resolved_ip, port, args.timeout)
            if ok:
                port_results.append(f"{port}:{rtt:.1f}ms")
            else:
                port_results.append(f"{port}:×({err})")
        
        rows.append([
            a.get("region", ""),
            a.get("carrier_name", ""),
            a.get("code", ""),
            resolved_ip,
            ", ".join(port_results),
        ])

    headers = ["地區", "運營商", "代碼", "探測 IP", "端口 RTT 結果"]
    col_widths = [len(h) for h in headers]
    for r in rows:
        for idx, c in enumerate(r):
            w = sum(2 if ord(ch) > 127 else 1 for ch in str(c))
            if w > col_widths[idx]:
                col_widths[idx] = w

    header_fmt = "  ".join(f"{{:<{w}}}" for w in col_widths)
    sep = "  ".join("-" * w for w in col_widths)
    print("\n" + header_fmt.format(*headers))
    print(sep)
    for r in rows:
        print(header_fmt.format(*r))
    print("=" * 70)


if __name__ == "__main__":
    main()
