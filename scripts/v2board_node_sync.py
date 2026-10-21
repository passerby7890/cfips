#!/usr/bin/env python3
"""
V2Board / Xboard 節點入口純 IP 極簡自動替換器 (v2.3.0)

核心要素：
  1. V2Board 地址或 IP + 管理員 Token
  2. 三張優選表 (ct 電信 / cu 聯通 / cm 移動) URL 或本地路徑
  3. 替換目標：節點 ID (node ID) + 協議 (預設 vless) + 綁定表 (ct/cu/cm)
  4. 僅替換入口地址 (server 欄位)，100% 嚴格原樣保留 port, uuid, tls_settings, network_settings (Host/SNI)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("v2board_sync")

DEFAULT_TABLES = {
    "ct": "https://raw.githubusercontent.com/passerby7890/cfips/main/output/ct.txt",
    "cu": "https://raw.githubusercontent.com/passerby7890/cfips/main/output/cu.txt",
    "cm": "https://raw.githubusercontent.com/passerby7890/cfips/main/output/cm.txt",
}

IP_REGEX = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3})")


def fetch_ip_table(source: str) -> List[Tuple[str, str]]:
    """自本地檔案或 HTTP URL 讀取 IP 清單，回傳 [(ip, country_tag), ...]"""
    content = ""
    # 1. 嘗試本地檔案
    p = Path(source)
    if p.is_file():
        try:
            content = p.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            logger.warning("讀取本地表 %s 失敗: %s", source, e)

    # 2. 嘗試網路下載
    if not content and (source.startswith("http://") or source.startswith("https://")):
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            req = urllib.request.Request(source, headers={"User-Agent": "curl/7.88.1"})
            with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
                content = resp.read().decode("utf-8", errors="ignore")
        except Exception as e:
            logger.error("下載遠端表 %s 失敗: %s", source, e)

    results = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tag = ""
        if "#" in line:
            parts = line.split("#", 1)
            line = parts[0].strip()
            tag = parts[1].strip().upper()
        m = IP_REGEX.match(line)
        if m:
            results.append((m.group(1), tag or "CF"))
    return results


class V2BoardAPI:
    """V2Board / Xiao 版面板管理員 API Client"""

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        # 建立忽略 SSL 憑證報錯的 context (支援自簽憑證或直接用 IP 存取)
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE

    def _headers(self) -> Dict[str, str]:
        h = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) V2BoardSync/2.3.0",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.token:
            # 同時帶入 Authorization 與 auth-data 以相容官方與 Xiao 魔改版
            h["Authorization"] = self.token
            h["auth-data"] = self.token
        return h

    def get_nodes(self, protocol: str = "vless") -> List[Dict[str, Any]]:
        """取得指定協議的全部節點清單"""
        url = f"{self.base_url}/api/v1/admin/server/{protocol}/fetch"
        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=15, context=self.ctx) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                data = res.get("data", [])
                if isinstance(data, list):
                    return data
                return []
        except urllib.error.HTTPError as e:
            # 備用 endpoint
            alt_url = f"{self.base_url}/api/v1/admin/server/manage/getNodes"
            req2 = urllib.request.Request(alt_url, headers=self._headers(), method="GET")
            with urllib.request.urlopen(req2, timeout=15, context=self.ctx) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                data = res.get("data", [])
                return [n for n in data if n.get("type") == protocol or protocol in str(n.get("name", "")).lower()]

    def update_node_server(self, protocol: str, node: Dict[str, Any], new_ip: str) -> bool:
        """只替換 node 的 server 欄位，原樣保留其他所有欄位並保存"""
        url = f"{self.base_url}/api/v1/admin/server/{protocol}/save"
        payload = dict(node)
        payload["server"] = new_ip

        encoded = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=encoded, headers=self._headers(), method="POST")
        with urllib.request.urlopen(req, timeout=15, context=self.ctx) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            return res.get("data") is True or res.get("data") == 1 or "success" in str(res).lower()


def select_best_ip(ip_list: List[Tuple[str, str]], preferred_country: str = "", rank: int = 1) -> Optional[Tuple[str, str]]:
    """篩選最佳 IP"""
    if not ip_list:
        return None
    idx = max(0, rank - 1)
    if preferred_country:
        filtered = [item for item in ip_list if item[1].upper() == preferred_country.upper()]
        if filtered:
            idx = min(idx, len(filtered) - 1)
            return filtered[idx]
    idx = min(idx, len(ip_list) - 1)
    return ip_list[idx]


def perform_sync(
    v2board_url: str,
    v2board_token: str,
    tables: Dict[str, str],
    nodes_to_update: List[Dict[str, Any]],
    dry_run: bool = False
) -> bool:
    print("======================================================================")
    print("🚀 V2Board / Xboard 節點入口純 IP 替換引擎")
    print(f"[*] 面板目標: {v2board_url}")
    print("======================================================================")

    # 1. 預先載入三張表
    table_cache: Dict[str, List[Tuple[str, str]]] = {}
    for t_name, t_url in tables.items():
        print(f"[*] 載入 [{t_name.upper()}] 表: {t_url}")
        ips = fetch_ip_table(t_url)
        table_cache[t_name] = ips
        print(f"    -> 成功載入 {len(ips)} 個極品 IP")

    # 2. 獲取節點清單
    api = V2BoardAPI(base_url=v2board_url, token=v2board_token)
    nodes_by_proto: Dict[str, Dict[int, Dict[str, Any]]] = {}

    protocols = list({item.get("protocol", "vless").lower() for item in nodes_to_update})
    for proto in protocols:
        if not dry_run:
            try:
                print(f"[*] 正在從面板獲取 [{proto}] 節點清單...")
                node_list = api.get_nodes(proto)
                nodes_by_proto[proto] = {int(n["id"]): n for n in node_list if "id" in n}
                print(f"    -> 獲取到 {len(node_list)} 個節點")
            except Exception as e:
                logger.error("無法獲取 [%s] 節點: %s", proto, e)
                print(f"❌ 無法連接面板或 Token 無效: {e}")
                return False
        else:
            # Dry-run 模擬節點 (以使用者面板常見節點為例)
            nodes_by_proto[proto] = {
                7: {"id": 7, "name": "JP 电信专用-日本(3倍)", "server": "www.090902.xyz", "port": 2053},
                6: {"id": 6, "name": "HK 移动专用-香港(3倍)", "server": "cf.godns.cc", "port": 2053}
            }

    # 3. 執行替換
    print("----------------------------------------------------------------------")
    success_count = 0
    for item in nodes_to_update:
        node_id = int(item["id"])
        proto = str(item.get("protocol", "vless")).lower()
        t_name = str(item.get("table", "ct")).lower()
        pref_country = str(item.get("country", "")).upper()
        rank = int(item.get("rank", 1))

        node_dict = nodes_by_proto.get(proto, {}).get(node_id)
        if not node_dict:
            print(f"❌ 找不到節點 ID [{node_id}] (協議: {proto})，略過！")
            continue

        name = node_dict.get("name", f"Node-{node_id}")
        curr_server = str(node_dict.get("server", "")).strip()
        port = node_dict.get("port", 443)

        # 取對應表的最佳 IP
        ip_pool = table_cache.get(t_name, [])
        best_choice = select_best_ip(ip_pool, preferred_country=pref_country, rank=rank)
        if not best_choice:
            print(f"❌ 表 [{t_name}] 中無可用 IP！(節點 ID: {node_id})")
            continue

        new_ip, tag = best_choice
        print(f"[*] 節點 ID: {node_id} 【{name}】")
        print(f"    - 原入口地址 (server) : {curr_server}:{port}")
        print(f"    - 綁定來源表         : {t_name.upper()} 表 (#{tag})")
        print(f"    - 最佳純 IP          : {new_ip} (#{tag})")

        if curr_server == new_ip:
            print(f"    - 執行結果           : ⚡ 目前已是最新 IP ({new_ip})，無需更換！")
            success_count += 1
            print("----------------------------------------------------------------------")
            continue

        if dry_run:
            print(f"    - 執行結果           : 🔍 [DRY-RUN 模擬] 預計將入口改為: {new_ip}")
            success_count += 1
            print("----------------------------------------------------------------------")
            continue

        try:
            ok = api.update_node_server(proto, node_dict, new_ip)
            if ok:
                print(f"    - 執行結果           : ✅ 成功替換！{curr_server} ➡️ {new_ip}")
                success_count += 1
            else:
                print(f"    - 執行結果           : ❌ 面板拒絕保存，請檢查 Token 權限！")
        except Exception as e:
            print(f"    - 執行結果           : ❌ 更新例外錯誤: {e}")
        print("----------------------------------------------------------------------")

    print(f"🎉 執行完畢！成功處理 {success_count}/{len(nodes_to_update)} 個節點。")
    print("======================================================================")
    return True


def interactive_wizard(config_path: Path) -> Dict[str, Any]:
    """互動式引導設定"""
    print("\n👋 檢測到尚未配置完整設定檔，啟動快速設定精靈：")
    url = input("1. 請輸入 V2Board 面板地址或 IP (如 https://panel.com 或 http://1.2.3.4): ").strip()
    token = input("2. 請輸入 管理員 Token: ").strip()
    
    print("\n3. 設定要替換的節點 (可稍後在 config.json 增加)：")
    node_id_str = input("   請輸入 節點 ID (如 7): ").strip()
    node_id = int(re.findall(r"\d+", node_id_str)[0]) if re.findall(r"\d+", node_id_str) else 7
    proto = input("   請輸入 協議 [預設: vless]: ").strip() or "vless"
    table = input("   請輸入 綁定哪張表 (ct/cu/cm) [預設: ct]: ").strip().lower() or "ct"
    country = input("   請輸入 偏好落地國 (JP/HK/SG/US，留空則自動): ").strip().upper()

    cfg = {
        "v2board": {
            "url": url,
            "token": token
        },
        "tables": DEFAULT_TABLES,
        "nodes": [
            {
                "id": node_id,
                "protocol": proto,
                "table": table,
                "country": country,
                "rank": 1
            }
        ]
    }
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"✅ 設定檔已自動儲存至: {config_path}")
    return cfg


def main():
    parser = argparse.ArgumentParser(description="V2Board (Xiao 版) 節點入口純 IP 替換工具")
    parser.add_argument("--config", "-c", default="config/v2board_sync.json", help="設定檔路徑")
    parser.add_argument("--url", help="覆蓋 V2Board 地址或 IP")
    parser.add_argument("--token", help="覆蓋 V2Board 管理員 Token")
    parser.add_argument("--node", "-n", type=int, help="指定替換的單一節點 ID (例如: 7)")
    parser.add_argument("--protocol", "-p", default="vless", help="指定節點協議 (預設: vless)")
    parser.add_argument("--table", "-t", choices=["ct", "cu", "cm"], help="指定使用的表 (ct/cu/cm)")
    parser.add_argument("--country", help="指定偏好落地國 (JP/HK/SG/US)")
    parser.add_argument("--dry-run", action="store_true", help="模擬執行 (不實際寫入面板)")

    args = parser.parse_args()

    cfg_file = Path(args.config).resolve()
    conf: Dict[str, Any] = {}

    if cfg_file.is_file():
        try:
            conf = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error("讀取設定檔失敗: %s", e)

    # 若命令列有傳入 node 參數，優先使用命令列參數
    if args.node is not None:
        nodes = [{
            "id": args.node,
            "protocol": args.protocol,
            "table": args.table or "ct",
            "country": args.country or "",
            "rank": 1
        }]
    else:
        nodes = conf.get("nodes", [])

    url = args.url or conf.get("v2board", {}).get("url") or conf.get("panel", {}).get("url", "")
    token = args.token or conf.get("v2board", {}).get("token") or conf.get("panel", {}).get("api_token", "")
    tables = conf.get("tables", DEFAULT_TABLES)

    # 若未提供 URL 與 Token，且不是 dry-run，進入互動引導
    if (not url or not token) and not args.dry_run:
        if sys.stdin.isatty():
            conf = interactive_wizard(cfg_file)
            url = conf["v2board"]["url"]
            token = conf["v2board"]["token"]
            nodes = conf.get("nodes", [])
            tables = conf.get("tables", DEFAULT_TABLES)
        else:
            logger.error("缺少 v2board url 或 token，請在 config.json 中配置！")
            sys.exit(1)

    if not nodes:
        logger.error("未指定任何要替換的節點 ID！")
        sys.exit(1)

    success = perform_sync(
        v2board_url=url or "http://127.0.0.1",
        v2board_token=token or "dummy_token",
        tables=tables,
        nodes_to_update=nodes,
        dry_run=args.dry_run
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
