#!/usr/bin/env python3
"""
=============================================================================
V2Board Syncer - 本地持久化狀態與黑名單記憶庫 (state_manager.py)
=============================================================================
功能：
  1. 【1034 邊緣阻斷記憶庫】：記錄觸發 Cloudflare Error 1034 的 IP，24 小時內跳過不測。
  2. 【超時 / GFW 封鎖 / Anycast 漂移繞美冷卻池】：記錄握手超時、境內探針 100% 丟包或實測延遲超標 (>115ms) 的 IP，冷卻 2 小時。
  3. 【節點指派歷史追蹤】：記錄各 node_id 當前綁定 IP、指派時間 (assigned_at)，供 Top-K 黏性調度使用。
"""

import json
import os
import time
from typing import Any, Dict, Optional

STATE_FILE = os.path.join(os.path.dirname(__file__), "state_cache.json")


class StateManager:
    def __init__(self, path: str = STATE_FILE):
        self.path = path
        self.data: Dict[str, Any] = {
            "error_1034_blacklist": {},  # ip -> timestamp
            "timeout_cooldown": {},      # ip -> timestamp
            "node_state": {}             # str(node_id) -> {"ip": str, "country": str, "assigned_at": float}
        }
        self.load()

    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        self.data.update(loaded)
            except Exception as e:
                print(f"[!] 讀取 state_cache.json 警告: {e}")
        self.cleanup_expired()

    def cleanup_expired(self):
        now = time.time()
        # 1034 邊緣阻斷黑名單：記憶 24 小時 (86400s)
        self.data["error_1034_blacklist"] = {
            ip: ts for ip, ts in self.data.get("error_1034_blacklist", {}).items()
            if now - ts < 86400
        }
        # 超時 / GFW 封鎖冷卻池：記憶 2 小時 (7200s)
        self.data["timeout_cooldown"] = {
            ip: ts for ip, ts in self.data.get("timeout_cooldown", {}).items()
            if now - ts < 7200
        }

    def is_blacklisted(self, ip: str) -> bool:
        return (
            ip in self.data.get("error_1034_blacklist", {})
            or ip in self.data.get("timeout_cooldown", {})
        )

    def get_blacklist_reason(self, ip: str) -> str:
        now = time.time()
        if ip in self.data.get("error_1034_blacklist", {}):
            rem = int(86400 - (now - self.data["error_1034_blacklist"][ip]))
            return f"1034 邊緣阻斷黑名單 (剩餘 {rem // 60} 分鐘)"
        if ip in self.data.get("timeout_cooldown", {}):
            rem = int(7200 - (now - self.data["timeout_cooldown"][ip]))
            return f"超時/被牆冷卻中 (剩餘 {rem // 60} 分鐘)"
        return ""

    def mark_1034(self, ip: str):
        self.data.setdefault("error_1034_blacklist", {})[ip] = time.time()

    def mark_timeout_or_gfw(self, ip: str):
        self.data.setdefault("timeout_cooldown", {})[ip] = time.time()

    def get_node_state(self, node_id: int) -> Dict[str, Any]:
        return self.data.get("node_state", {}).get(str(node_id), {})

    def set_node_state(self, node_id: int, ip: str, country: str = ""):
        self.data.setdefault("node_state", {})[str(node_id)] = {
            "ip": ip,
            "country": country,
            "assigned_at": time.time()
        }

    def save(self):
        try:
            self.cleanup_expired()
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[!] 無法寫入 state_cache.json: {e}")
