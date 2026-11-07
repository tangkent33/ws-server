"""Miaaoo 聯機大廳 API（房間列表 / 加入）。"""
from typing import Any, Dict, List, Optional

import aiohttp

from .config import MIAOO_API_BASE, MC_PLAYER_NAME, log


class MiaaooAPI:
    def __init__(self):
        self.base_url = MIAOO_API_BASE
        self.xuid: Optional[str] = None
        self.addid: Optional[str] = None
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        if not self._session:
            self._session = aiohttp.ClientSession()

    async def close(self):
        if self._session:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------ 基本請求
    async def _get(self, endpoint: str, params: Optional[Dict] = None) -> Any:
        if not self._session:
            await self.start()
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        async with self._session.get(url, params=params, timeout=10) as resp:
            return await resp.json()

    async def _post(self, endpoint: str, data: Dict) -> Any:
        if not self._session:
            await self.start()
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        async with self._session.post(url, json=data, timeout=10) as resp:
            return await resp.json()

    # ------------------------------------------------------------ 端點
    async def get_account_info(self) -> Dict[str, Any]:
        return await self._get("v2/account")

    async def get_profile(self, gt: Optional[str] = None, xuid: Optional[str] = None) -> Dict[str, Any]:
        params = {}
        if gt:
            params["gt"] = gt
        if xuid:
            params["xuid"] = xuid
        return await self._get("profile", params)

    async def get_room_info(self, session: Optional[str] = None, roomfrom: Optional[str] = None) -> Dict[str, Any]:
        params = {}
        if session:
            params["session"] = session
        if roomfrom:
            params["roomfrom"] = roomfrom
        return await self._get("roominfo", params)

    async def join_room_v2(self, roomid: str, sessionname: str, roomfrom: str,
                           addid: Optional[str] = None, xuid: Optional[str] = None) -> Dict[str, Any]:
        payload = {
            "version": "1.0.0",
            "joininformation": {
                "addid": addid or self.addid,
                "roomfrom": roomfrom,
                "roomid": roomid,
                "sessionname": sessionname,
            },
            "invitecontrol": {"userxuid": xuid or self.xuid},
        }
        return await self._post("v2/join", payload)

    async def refresh_followers(self) -> Dict[str, Any]:
        return await self._post("refreshFollowers", {})

    # ------------------------------------------------------------ 組合查詢
    async def list_rooms(self) -> List[Dict]:
        try:
            result = await self.get_room_info()
            if isinstance(result, dict):
                rooms = result.get("results") or result.get("data") or []
                if isinstance(rooms, list):
                    return rooms
                if "room" in result:
                    return [result]
                return []
            if isinstance(result, list):
                return result
            return []
        except Exception as e:
            log.error(f"獲取房間列表失敗: {e}")
            return []

    async def get_my_xuid_and_addid(self):
        xuid, addid = "", ""
        account = None
        try:
            account = await self.get_account_info()
            if isinstance(account, dict):
                addid = account.get("addid") or account.get("addId") or account.get("id") or ""
                if not addid and isinstance(account.get("data"), dict):
                    addid = account["data"].get("addid") or account["data"].get("id") or ""
            elif isinstance(account, list) and account and isinstance(account[0], dict):
                addid = account[0].get("addid") or account[0].get("addId") or account[0].get("id") or ""
        except Exception as e:
            log.error(f"獲取 AddID 失敗: {e}")

        try:
            gt = MC_PLAYER_NAME
            if isinstance(account, dict):
                gt = account.get("gamertag") or account.get("gt") or MC_PLAYER_NAME
                if not gt and isinstance(account.get("data"), dict):
                    gt = account["data"].get("gamertag") or MC_PLAYER_NAME
            profile = await self.get_profile(gt=gt)
            if isinstance(profile, dict):
                xuid = profile.get("xuid") or profile.get("XUID") or ""
                if not xuid and isinstance(profile.get("data"), dict):
                    xuid = profile["data"].get("xuid") or ""
        except Exception as e:
            log.error(f"獲取 XUID 失敗: {e}")

        return xuid, addid
