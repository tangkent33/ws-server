"""Minecraft 文字處理：色碼、rawtext JSON、UI 洗頻。"""
import json
import re

COLOR_CODE = re.compile(r"§.")

# 伺服器 UI 廣播（tellraw 狀態面板之類），不要往 Discord 送
UI_SPAM_KEYWORDS = ("系統狀態", "掃地倒數", "你的錢包", "VIP等級", "當前人數", "你的名字")


def strip_colors(text: str) -> str:
    """移除 §a §l 這類色碼。"""
    return COLOR_CODE.sub("", str(text))


def extract_text(raw: str) -> str:
    """把 Minecraft 的 § 色碼與 rawtext JSON 拆成純文字。"""
    text = strip_colors(raw).strip()

    if text.startswith("{") and "rawtext" in text:
        try:
            data = json.loads(text)
        except Exception:
            return ""
        parts = data.get("rawtext")
        if isinstance(parts, list):
            return "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
        if isinstance(parts, str):
            return parts
    return text


def is_ui_spam(text: str) -> bool:
    return any(k in text for k in UI_SPAM_KEYWORDS)
