"""JSON 檔案型資料存放：白名單、封禁名單。"""
import json
import os
import time
from typing import Dict, List, Optional

from .config import log


class WhitelistStore:
    """授權使用者（Discord ID 或名稱）。"""

    def __init__(self, path: str, defaults: List[str]):
        self.path = path
        self.defaults = list(defaults)
        self._data: List[str] = []
        self.load()

    def load(self) -> List[str]:
        if not os.path.exists(self.path):
            self._data = list(self.defaults)
            self.save()
            return self._data
        try:
            with open(self.path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            self._data = [str(x) for x in data] if isinstance(data, list) else list(self.defaults)
        except Exception:
            self._data = list(self.defaults)
        return self._data

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=4, ensure_ascii=False)

    @property
    def users(self) -> List[str]:
        return self._data


class BanStore:
    """封禁名單：{名字: {reason, until(ms), by}}。

    until 為 None 代表永久；每次查詢時檢查是否過期，過期就自動移除。
    """

    def __init__(self, path: str):
        self.path = path
        self._data: Dict[str, dict] = {}
        self.load()

    # ------------------------------------------------ 讀寫
    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except Exception:
            self._data = {}
            return

        if isinstance(data, list):          # 舊格式（純陣列）→ 轉成永久封禁
            self._data = {
                str(x).lower(): {"name": str(x), "reason": "黑名單", "until": None, "by": "legacy"}
                for x in data
            }
        elif isinstance(data, dict):
            self._data = {str(k).lower(): v for k, v in data.items()}
        else:
            self._data = {}

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            log.error(f"寫入封禁名單失敗: {e}")

    # ------------------------------------------------ 查詢
    @staticmethod
    def _key(name: str) -> str:
        return str(name).strip().lower()

    def get(self, name: str) -> Optional[dict]:
        key = self._key(name)
        rec = self._data.get(key)
        if not rec:
            return None
        if rec.get("until") and time.time() * 1000 > rec["until"]:
            del self._data[key]
            self.save()
            log.info(f"⏰ {name} 封禁到期，自動解封")
            return None
        return rec

    def contains(self, name: str) -> bool:
        return self.get(name) is not None

    def items(self) -> List[dict]:
        return [rec for rec in (self.get(k) for k in list(self._data.keys())) if rec]

    # ------------------------------------------------ 異動
    def ban(self, name: str, reason: str = "", minutes: int = 0, by: str = "api") -> dict:
        rec = {
            "name": name,
            "reason": reason or "未填理由",
            "until": int(time.time() * 1000) + minutes * 60000 if minutes else None,
            "by": by,
        }
        self._data[self._key(name)] = rec
        self.save()
        return rec

    def unban(self, name: str) -> bool:
        key = self._key(name)
        if key in self._data:
            del self._data[key]
            self.save()
            return True
        return False


class BridgeConfig:
    """允許的 Discord 伺服器 / 頻道。

    兩層設定：
      - .env 的 ALLOWED_GUILD_ID / ALLOWED_CHANNEL_ID（可多個，清單）
      - bridge_config.json（單一值，由 !setguild / !setchannel 寫入，優先）
    """

    def __init__(self, path: str, default_guilds: List[int], default_channels: List[int]):
        self.path = path
        self.guild_ids = list(default_guilds)
        self.channel_ids = list(default_channels)
        self.guild_id = 0
        self.channel_id = 0
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except Exception:
            return
        try:
            self.guild_id = int(data.get("guild") or 0)
            self.channel_id = int(data.get("channel") or 0)
        except (TypeError, ValueError):
            pass

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({"guild": self.guild_id, "channel": self.channel_id}, f, indent=4)
        except Exception as e:
            log.error(f"儲存 bridge_config.json 失敗: {e}")
