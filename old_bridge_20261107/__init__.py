"""bridge — Discord ⇄ Minecraft 橋接套件.

模組分層：
    config      設定與日誌
    jsonstore   白名單 / 封禁名單（JSON 檔案）
    textutil    Minecraft 文字清洗
    mcws        WebSocket 伺服器（收事件、送指令）
    banapi      HTTP 封禁 API
    discordbot  Discord 客戶端
    app         組裝與啟動
"""

__version__ = "9.0.0"
