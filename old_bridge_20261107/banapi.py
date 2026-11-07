"""HTTP 封禁 API（只綁 127.0.0.1）。

    GET  /is-banned/<名字>      公開查詢
    GET  /banned?key=...        封禁名單
    POST /ban                   {"userId","reason","minutes"}
    POST /unban                 {"userId"}
"""
import asyncio

from aiohttp import web

from .config import BAN_API_KEY, BAN_API_PORT, log


def _authorized(request: web.Request) -> bool:
    # header 或瀏覽器友善的 ?key=
    return request.headers.get("x-api-key") == BAN_API_KEY or request.query.get("key") == BAN_API_KEY


def _unauthorized() -> web.Response:
    return web.json_response({"error": "無效 API key"}, status=401)


async def start_ban_api(bot, store):
    """啟動 Ban API；回傳 runner，呼叫端負責在結束時 cleanup。"""

    async def is_banned(request: web.Request):
        name = request.match_info["name"]
        rec = store.get(name)
        return web.json_response({"userId": name, "banned": bool(rec), **(rec or {})})

    async def list_banned(request: web.Request):
        if not _authorized(request):
            return _unauthorized()
        items = store.items()
        return web.json_response({"count": len(items), "list": items})

    async def ban(request: web.Request):
        if not _authorized(request):
            return _unauthorized()

        data = await request.json()
        name = (data or {}).get("userId")
        if not name:
            return web.json_response({"error": "要填 userId"}, status=400)

        minutes = int(data.get("minutes") or 0)
        rec = store.ban(name, data.get("reason", ""), minutes, "api")
        asyncio.create_task(bot.kick_player(name, rec["reason"]))
        log.info(f"🔨 API 封禁 {name}: {rec['reason']}")
        return web.json_response({"ok": True, "userId": name, **rec})

    async def unban(request: web.Request):
        if not _authorized(request):
            return _unauthorized()

        data = await request.json()
        name = (data or {}).get("userId")
        if not name:
            return web.json_response({"error": "要填 userId"}, status=400)

        ok = store.unban(name)
        return web.json_response({"ok": ok, "userId": name}, status=200 if ok else 404)

    app = web.Application()
    app.add_routes([
        web.get("/is-banned/{name}", is_banned),
        web.get("/banned", list_banned),
        web.post("/ban", ban),
        web.post("/unban", unban),
    ])

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", BAN_API_PORT)
    await site.start()
    log.info(f"✅ Ban API 已啟動 http://127.0.0.1:{BAN_API_PORT} (x-api-key: {BAN_API_KEY})")
    return runner
