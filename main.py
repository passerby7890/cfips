#!/usr/bin/env python3
"""CF-Global-QuadNet-Feed 主程式入口 (v2.0 終極修訂版)。

- 階段一：ECS DoH 偽裝解析 + 境內實測榜單 + 分國源 + 本地種子
- 階段二：同 /24 網段冷門鄰居平移抽樣
- 階段三：0.6s 極短超時 TCP 快篩 + 端到端 TLS + WebSocket 101 回源測活
- 階段四：10MB 切片 2.5 秒限時吞吐測速快篩
- 階段五：四層階梯降級、EWMA 歷史評分衰減、三網 Top 50 輸出 (output/ct.txt, cu.txt, cm.txt)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from src import __version__
from src.collector import collect_all_candidates
from src.exporter import export_all
from src.neighbor_shifter import shift_neighbors
from src.speed_tester import batch_measure_bandwidth
from src.verifier import OriginVerifier

PROJECT_ROOT = Path(__file__).resolve().parent

# 日誌格式設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cf-global-quadnet-feed",
        description="Cloudflare 三網前 50 優選 IP 自動化雲端生成系統",
    )
    parser.add_argument(
        "-c", "--config",
        default="config/config.yaml",
        help="設定檔路徑（預設 config/config.yaml）",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="覆寫測活併發數（預設 40）",
    )
    parser.add_argument(
        "--no-ecs",
        action="store_true",
        help="停用 ECS DoH 偽裝解析策略",
    )
    parser.add_argument(
        "--no-neighbor",
        action="store_true",
        help="停用同 /24 網段冷門鄰居平移演算法",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="模擬執行模式（輸出報表但不覆寫實際檔案）",
    )
    parser.add_argument(
        "-V", "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser.parse_args()


def load_config(cfg_path: Path) -> dict[str, Any]:
    if not cfg_path.is_file():
        sys.exit(f"[-] 設定檔不存在：{cfg_path}")
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        return data
    except Exception as exc:
        sys.exit(f"[-] 設定檔 YAML 解析失敗：{exc}")


def load_anchors(anchors_path: Path) -> list[dict[str, Any]]:
    if not anchors_path.is_file():
        log.warning("錨點檔案不存在：%s", anchors_path)
        return []
    try:
        data = json.loads(anchors_path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "anchors" in data:
            return data["anchors"]
        elif isinstance(data, dict):
            res = []
            for k, v in data.items():
                carrier = v.get("carrier", "")
                if "电信" in carrier or "電信" in carrier or carrier == "ct":
                    c_code = "ct"
                elif "联通" in carrier or "聯通" in carrier or carrier == "cu":
                    c_code = "cu"
                elif "移动" in carrier or "移動" in carrier or carrier == "cm":
                    c_code = "cm"
                else:
                    c_code = "edu"
                res.append({
                    "code": k,
                    "region": v.get("city", ""),
                    "carrier": c_code,
                    "ip": v.get("ip", ""),
                    "ipv4": v.get("ip", ""),
                    "domain": v.get("domain", ""),
                })
            return res
        elif isinstance(data, list):
            return data
        return []
    except Exception as exc:
        log.error("錨點檔案解析失敗：%s", exc)
        return []


def print_table(title: str, headers: list[str], rows: list[list[str]]) -> None:
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


async def run_pipeline(args: argparse.Namespace) -> int:
    started_at = time.time()
    cfg_path = (PROJECT_ROOT / args.config).resolve()
    config = load_config(cfg_path)

    # 參數覆寫與調優設定
    tuning = config.get("tuning", {})
    concurrency = args.concurrency or int(tuning.get("concurrency_limit", config.get("concurrency", 40)))
    precheck_timeout = float(tuning.get("precheck_timeout_sec", 0.6))
    speedtest_duration = float(tuning.get("speedtest_duration_limit", 2.5))

    if args.no_ecs:
        config.setdefault("collector", {}).setdefault("ecs_doh", {})["enabled"] = False
    if args.no_neighbor:
        config.setdefault("neighbor_shifter", {})["enabled"] = False

    anchors_rel = config.get("collector", {}).get("ecs_doh", {}).get("anchors_file", "data/cn_anchors.json")
    anchors = load_anchors((PROJECT_ROOT / anchors_rel).resolve())

    origin_cfg = config.get("origin", {})
    log.info("=" * 65)
    log.info("CF-Global-QuadNet-Feed v%s 啟動", __version__)
    log.info("目標源站：%s:%s (WS: %s, 預期 101)", origin_cfg.get('host'), origin_cfg.get('port'), origin_cfg.get('ws_path'))
    log.info("調優參數：併發 %d，快篩超時 %.1fs，測速截斷 %.1fs", concurrency, precheck_timeout, speedtest_duration)
    log.info("=" * 65)

    # 1. 階段一：多維度情報收集
    candidates = await collect_all_candidates(config, anchors, PROJECT_ROOT)

    # 2. 階段二：同 /24 網段冷門鄰居平移演算法
    expanded_candidates = shift_neighbors(candidates, config.get("neighbor_shifter", {}))

    # 3. 階段三：端到端真實回源測活 (0.6s TCP 快篩 + TLS + trace + WS 101)
    verifier = OriginVerifier(origin_cfg, concurrency=concurrency, precheck_timeout=precheck_timeout)
    results = await verifier.verify_all(expanded_candidates)

    verified_items = [r.to_dict() for r in results.values() if r.ok and r.ws_handshake_101]

    # 4. 階段四：10MB 切片 2.5 秒限時吞吐測速快篩
    if verified_items:
        verified_items = batch_measure_bandwidth(
            items=verified_items,
            port=int(origin_cfg.get("port", 2053)),
            sni_host=str(origin_cfg.get("sni", "node1.snowcat77.com")),
            duration_limit=speedtest_duration,
            max_workers=min(20, len(verified_items)),
        )

    # 5. 階段五：四層階梯降級、EWMA 歷史評分衰減與檔案輸出
    output_dir = PROJECT_ROOT / config.get("exporter", {}).get("output_dir", "output")
    if args.dry_run:
        output_dir = PROJECT_ROOT / "output_dryrun"
        log.info("模擬模式：輸出將儲存至臨時目錄 %s", output_dir)

    summary = export_all(verified_items, config, output_dir)

    # 6. 終端報表渲染 (三網前 50)
    carrier_rows = []
    carriers_meta = [
        ("ct", "中國電信 (CT)", "output/ct.txt"),
        ("cu", "中國聯通 (CU)", "output/cu.txt"),
        ("cm", "中國移動 (CM)", "output/cm.txt"),
    ]
    for c_key, c_name, out_file in carriers_meta:
        items = summary.get("by_carrier", {}).get(c_key, [])
        status = summary.get("carriers_status", {}).get(c_key, "UNKNOWN")
        if items:
            best_ip = f"{items[0]['ip']}#{items[0].get('country_code', 'US')}"
            colos = ",".join(sorted(set(it.get("colo", "") for it in items if it.get("colo"))))
            lats = [it["latency_ms"] for it in items if "latency_ms" in it]
            lat_range = f"{min(lats):.1f}~{max(lats):.1f}ms" if lats else "-"
            top_spd = items[0].get("speed_mbps") or items[0].get("download_speed", 0)
            top_loss = items[0].get("loss_percent") or items[0].get("loss_rate", 0)
            speed_info = f"{top_spd:.0f}Mbps / {top_loss:.1f}%"
            carrier_rows.append([c_name, out_file, str(len(items)), best_ip, colos, lat_range, speed_info, status])
        else:
            carrier_rows.append([c_name, out_file, "0", "-", "-", "-", "-", status])

    print_table("三網優選 Top 50 產出成果", ["運營商", "匯出檔案", "挑選數", "最佳節點 (Top 1)", "機房分佈", "握手延遲區間", "實測頻寬/丟包", "階梯狀態"], carrier_rows)

    elapsed = time.time() - started_at
    log.info("全流程執行完畢，總耗時: %.1f 秒，合格回源節點總數: %d 個", elapsed, summary["total_verified_ok"])
    return 0


def main() -> None:
    args = parse_args()
    try:
        code = asyncio.run(run_pipeline(args))
        sys.exit(code)
    except KeyboardInterrupt:
        log.info("使用者中斷執行")
        sys.exit(130)
    except Exception as exc:
        log.exception("執行過程發生未預期例外：%s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
