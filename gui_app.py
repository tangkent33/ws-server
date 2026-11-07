# -*- coding: utf-8 -*-
"""MC 聊天橋接控制台 —— 桌面版（pywebview 原生視窗）。

把 gui.html 的介面包成一個原生桌面視窗（不是瀏覽器分頁）。
它連的是 bot 開在本機的接口 http://127.0.0.1:8080，所以：
    - bot 沒在跑（沒跑 start.bat）→ 視窗會顯示「連不上」提示
    - bot 在跑 → 即時顯示狀態、玩家、記錄，也能直接下指令 / 踢人 / 封禁

打包成 exe（需要先 pip install pyinstaller）：
    py -3 -m PyInstaller --onefile --noconsole --name MC-Bridge-GUI gui_app.py
"""

import socket
import sys

import webview
import webview.platforms.edgechromium  # noqa: 讓 PyInstaller 收進 EdgeChromium 後端
import clr_loader  # noqa: pythonnet 的載入器，PyInstaller 打包需要

URL = "http://127.0.0.1:8080"

_OFFLINE_HTML = (
    "<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'></head>"
    "<body style='background:#1b1e24;color:#e8eaed;font-family:system-ui;"
    "display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>"
    "<div style='text-align:center'>"
    "<div style='font-size:48px'>🔌</div>"
    "<h2>連不上 bot</h2>"
    "<p>控制面板服務（127.0.0.1:8080）沒有回應。</p>"
    "<p style='color:#9aa3b2'>請先雙擊 <b>start.bat</b> 把 bot 跑起來，再開這個視窗。</p>"
    "</div></body></html>"
)


def port_open(port: int, timeout: float = 0.6) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def main() -> None:
    if port_open(8080):
        target, title = URL, "MC 聊天橋接控制台"
    else:
        target, title = ("data:text/html;charset=utf-8," + _OFFLINE_HTML), "MC 聊天橋接控制台（離線）"

    webview.create_window(
        title,
        target,
        width=1020,
        height=720,
        min_size=(760, 560),
        background_color="#1b1e24",
        text_select=True,
    )
    webview.start()


if __name__ == "__main__":
    main()
