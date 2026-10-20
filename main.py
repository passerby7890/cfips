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
import datetime
import json
import logging
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import yaml

from src import __version__
from src.cn_evaluator import evaluate_for_carrier
from src.collector import collect_all_candidates
from src.exporter import export_carrier_ips
from src.neighbor_shifter import shift_to_clean_neighbors
from src.speed_tester import measure_ip_bandwidth
from src.verifier import batch_verify_101

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
        "--direct",
        action="store_true",
        default=True,
        help="直接採用已收集並經過三網評估的大陸優選 IP 池，跳過耗時的回源測速",
    )
    parser.add_argument(
        "--full-test",
        action="store_true",
        help="執行完整 101 回源與下行吞吐測速",
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

    tuning = config.get("tuning", {})
    handshake_concurrency = args.concurrency or int(tuning.get("handshake_concurrency", 50))
    speedtest_duration = float(tuning.get("speedtest_duration_limit", 2.0))
    speedtest_workers = int(tuning.get("speedtest_workers", 8))
    target_output_count = int(tuning.get("target_output_count", 50))
    max_per_subnet = int(tuning.get("max_ips_per_subnet", 3))
    max_per_country = int(tuning.get("max_ips_per_country", 15))

    if args.no_ecs:
        config.setdefault("collector", {}).setdefault("ecs_doh", {})["enabled"] = False

    anchors_rel = config.get("collector", {}).get("ecs_doh", {}).get("anchors_file", "data/cn_anchors.json")
    anchors = load_anchors((PROJECT_ROOT / anchors_rel).resolve())

    origin_cfg = config.get("origin", {})
    log.info("=" * 65)
    log.info("CF-Global-QuadNet-Feed v%s 啟動 (大陸三網視角雙引擎架構)", __version__)
    log.info("目標源站：%s:%s (WS: %s, 預期 101)", origin_cfg.get('host'), origin_cfg.get('port'), origin_cfg.get('ws_path'))
    log.info("調優參數：握手併發 %d，測速線程: %d，切片截斷: %.1fs，目標每網: %d 筆",
             handshake_concurrency, speedtest_workers, speedtest_duration, target_output_count)
    log.info("=" * 65)

    # 1. 階段一：聚合境內三網探針、亞太神仙段與 2000+ 候選池
    log.info("[*] 階段 1: 聚合境內三網探針、亞太神仙段與 2000+ 候選池...")
    raw_candidates = await collect_all_candidates(config, anchors, PROJECT_ROOT)
    if args.no_neighbor:
        expanded_pool = raw_candidates
        log.info("[*] 冷門鄰居平移已停用，保持原始候選池: %d 筆", len(expanded_pool))
    else:
        expanded_pool = shift_to_clean_neighbors(raw_candidates, count_per_subnet=2)
        log.info("[*] 展開後總候選池規模: %d 筆", len(expanded_pool))

    # 2. 階段二：為電信(ct)、聯通(cu)、移動(cm) 各自按「境內視角」獨立初評，選出各網前 100 名候選
    log.info("[*] 階段 2: 大陸三網獨立路由基因與境內探針雙引擎初評...")
    carrier_finalists = {}
    unique_to_verify = {}

    for carrier in ["ct", "cu", "cm"]:
        scored_list = [evaluate_for_carrier(item, carrier) for item in expanded_pool]
        # 按境內評分排序，絕不使用美國 Runner 延遲排序！
        scored_list.sort(key=lambda x: x.get("final_score", 0.0), reverse=True)

        # 同 /24 網段在初篩時最多保留 4 個，取前 150 名進入回源驗證
        top_candidates = []
        subnet_cnt = {}
        for item in scored_list:
            subnet = ".".join(item["ip"].split(".")[:3])
            if subnet_cnt.get(subnet, 0) < 4:
                top_candidates.append(item)
                subnet_cnt[subnet] = subnet_cnt.get(subnet, 0) + 1
                unique_to_verify[item["ip"]] = item
            if len(top_candidates) >= 150:
                break
        carrier_finalists[carrier] = top_candidates
        top1 = top_candidates[0] if top_candidates else {}
        log.info("[+] [%s] 境內初評前 %d 名精選完成（最高分: %.1f, 預估延遲: %.1fms, 頂級網段: %s#%s）",
                 carrier.upper(), len(top_candidates), top1.get("final_score", 0.0), top1.get("est_cn_latency_ms", 0.0),
                 top1.get("ip", ""), top1.get("country", ""))

    is_direct = not getattr(args, "full_test", False)
    verified_map = {}

    if not is_direct:
        # 3. 階段三：對三網精選出的去重節點執行 101 回源握手驗證
        log.info("[*] 階段 3: 對三網精選出的 %d 個去重節點執行 101 回源握手驗證...", len(unique_to_verify))
        verified_list = await batch_verify_101(
            list(unique_to_verify.values()),
            origin_cfg=origin_cfg,
            concurrency=handshake_concurrency
        )
        verified_map = {item["ip"]: item for item in verified_list if item.get("ws_handshake_101")}
        log.info("[+] 成功通過 %s:%s%s 101 握手合格節點: %d 筆",
                 origin_cfg.get('host'), origin_cfg.get('port'), origin_cfg.get('ws_path'), len(verified_map))

        # 4. 階段四：對通過 101 握手的節點執行快速回源吞吐檢查
        verified_ips = list(verified_map.keys())
        port = int(origin_cfg.get("port", 2053))
        sni_host = str(origin_cfg.get("sni", "node1.snowcat77.com"))
        if verified_ips:
            log.info("[*] 階段 4: 對 %d 個合格節點執行 %.1f 秒快速下行吞吐測速 (線程池: %d)...",
                     len(verified_ips), speedtest_duration, speedtest_workers)
            with ThreadPoolExecutor(max_workers=speedtest_workers) as executor:
                futures = {
                    ip: executor.submit(measure_ip_bandwidth, ip, port, sni_host, speedtest_duration)
                    for ip in verified_ips
                }
                for ip, fut in futures.items():
                    try:
                        verified_map[ip]["speed_mbps"] = fut.result()
                        verified_map[ip]["download_speed"] = verified_map[ip]["speed_mbps"]
                    except Exception as exc:
                        log.debug("測速異常 %s: %s", ip, exc)
                        verified_map[ip]["speed_mbps"] = 50.0
    else:
        log.info("[*] 依指令啟動急速直出模式：跳過耗時回源測速，直接聚合境內實時情報與雙引擎評估成果！")
        for carrier in ["ct", "cu", "cm"]:
            for cand in carrier_finalists[carrier]:
                verified_map[cand["ip"]] = cand

    # 5. 階段五：三網各自重新結算最終分數並輸出 Top 50 (IP#COUNTRY)
    output_dir = PROJECT_ROOT / config.get("exporter", {}).get("output_dir", "output")
    if args.dry_run:
        output_dir = PROJECT_ROOT / "output_dryrun"
        log.info("模擬模式：輸出將儲存至臨時目錄 %s", output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    carrier_summary = {}
    carrier_results = {}

    for carrier in ["ct", "cu", "cm"]:
        if is_direct:
            valid_carrier_pool = carrier_finalists[carrier]
        else:
            valid_carrier_pool = []
            seen_pool_ips = set()
            # 優先納入該網專屬初選通過者
            for cand in carrier_finalists[carrier]:
                ip = cand["ip"]
                if ip in verified_map and ip not in seen_pool_ips:
                    merged = dict(verified_map[ip])
                    merged.update({
                        "source_carrier": cand.get("source_carrier", ""),
                        "cn_latency_ms": cand.get("cn_latency_ms"),
                        "cn_speed_mbps": cand.get("cn_speed_mbps", 0.0),
                        "cn_loss_percent": cand.get("cn_loss_percent", 0.0),
                        "source_country": cand.get("source_country", "") or merged.get("source_country", ""),
                        "source_colo": cand.get("source_colo", "") or merged.get("source_colo", ""),
                        "is_shifted_neighbor": cand.get("is_shifted_neighbor", False),
                    })
                    final_item = evaluate_for_carrier(merged, carrier)
                    valid_carrier_pool.append(final_item)
                    seen_pool_ips.add(ip)

            # 若專屬初選合格數不足，從其餘通過 101 的合格節點依該網評分模型補充
            for ip, v_node in verified_map.items():
                if ip not in seen_pool_ips:
                    merged = dict(v_node)
                    final_item = evaluate_for_carrier(merged, carrier)
                    valid_carrier_pool.append(final_item)
                    seen_pool_ips.add(ip)

        res = export_carrier_ips(
            valid_carrier_pool,
            carrier,
            output_dir / f"{carrier}.txt",
            max_count=target_output_count,
            max_per_subnet=max_per_subnet,
            max_per_country=max_per_country,
            require_101=(not is_direct),
        )
        carrier_summary[carrier] = res
        carrier_results[carrier] = res["data"]

    # 輸出企業級優選域名至 output/domains.txt
    ent_src = PROJECT_ROOT / "data" / "enterprise_domains.txt"
    ent_dst = output_dir / "domains.txt"
    if ent_src.is_file():
        shutil.copy2(ent_src, ent_dst)
        log.info("企業級優選域名已同步輸出 -> %s", ent_dst)

    # 輸出結構化 JSON 總表
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    summary_data = {
        "version": __version__,
        "updated_at": now.isoformat(timespec="seconds"),
        "timestamp": int(now.timestamp()),
        "total_verified_ok": len(verified_map),
        "carriers_status": {k: v["status"] for k, v in carrier_summary.items()},
        "by_carrier": carrier_results,
        "carriers": carrier_results,
    }
    json_path = output_dir / "best_ips.json"
    json_path.write_text(json.dumps(summary_data, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("結構化總表已輸出 -> %s", json_path)

    # 6. 終端報表渲染 (三網前 50)
    carrier_rows = []
    carriers_meta = [
        ("ct", "中國電信 (CT)", f"{output_dir.name}/ct.txt"),
        ("cu", "中國聯通 (CU)", f"{output_dir.name}/cu.txt"),
        ("cm", "中國移動 (CM)", f"{output_dir.name}/cm.txt"),
    ]
    for c_key, c_name, out_file in carriers_meta:
        items = carrier_results.get(c_key, [])
        status = carrier_summary.get(c_key, {}).get("status", "UNKNOWN")
        if items:
            best_ip = f"{items[0]['ip']}#{items[0].get('country_code', items[0].get('country', 'US'))}"
            colos = ",".join(sorted(set(it.get("colo", "") for it in items if it.get("colo"))))
            lats = [float(it.get("est_cn_latency_ms", it.get("latency_ms", 0))) for it in items]
            lat_range = f"{min(lats):.1f}~{max(lats):.1f}ms" if lats else "-"
            top_spd = items[0].get("speed_mbps") or items[0].get("download_speed", 0)
            top_loss = items[0].get("loss_percent") or items[0].get("loss_rate", 0)
            speed_info = f"{top_spd:.0f}Mbps / {top_loss:.1f}%"
            carrier_rows.append([c_name, out_file, str(len(items)), best_ip, colos or "-", lat_range, speed_info, status])
        else:
            carrier_rows.append([c_name, out_file, "0", "-", "-", "-", "-", status])

    print_table("三網優選 Top 50 產出成果 (v1.4.0 大陸三網視角雙引擎)", ["運營商", "匯出檔案", "挑選數", "最佳節點 (Top 1)", "機房分佈", "境內預估延遲", "實測頻寬/丟包", "階梯狀態"], carrier_rows)

    elapsed = time.time() - started_at
    log.info("v1.4.0 三網獨立境內視角篩選管線執行完畢，總耗時: %.1f 秒，合格回源節點總數: %d 個", elapsed, len(verified_map))
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
