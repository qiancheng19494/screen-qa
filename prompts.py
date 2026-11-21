# -*- coding: utf-8 -*-
"""集中放提示词/界面长文案（UTF-8 独立文件，避免内联中文被控制台编码弄坏）。"""

FOLLOWUP_SYSTEM = (
    "你是数学助教。用户会给你一段新框选的内容，它是针对同一道题的追问、第二问、"
    "或他自己写的步骤。请只根据给出的内容作答，不要重复整道题；如果内容不足以回答，"
    "直接说『框选的内容看不出问题』。回答控制在 6 行以内，用中文。"
)

FOLLOWUP_TOO_LITTLE = "框到的内容太少（不到 4 个字），框大一点试试"

LBL_FOLLOWUP = "追问"
LBL_RECENT = "最近图"
MSG_FOLLOWUP_HINT = "追问：框住题目的一部分，或你写的步骤"
MSG_NEED_PREV = "先做一道题，再按追问（Ctrl+Alt+F）"
MSG_CONSIST_WARN = "⚠ 与上次答案不同：{old} → {new}（Ctrl+Alt+R 重算）"
MSG_SLOW = "⚠ 已经等了 {sec:.0f} 秒还没回来，模型可能卡了（可重算或稍等）"
MSG_FOLLOWUP_TITLE = "追问 · {sec:.0f}s"
MSG_REGION_SAVED = "已记住区域：{name}"
MSG_REGION_EMPTY = "还没有记住的区域（先圈一次，再点『记住当前区域』）"

# ---------------- 界面文案（中英） ----------------
LBL = {
    "title": ("屏幕问答 · DeepSeek", "Screen QA · DeepSeek"),
    "capture": ("框选截图", "Capture"),
    "full": ("全屏", "Full"),
    "paste": ("粘贴", "Paste"),
    "bad": ("错了", "Wrong"),
    "steps": ("步骤", "Steps"),
    "copy": ("复制答案", "Copy"),
    "plot": ("图像", "Plot"),
    "speak": ("朗读", "Speak"),
    "mini": ("迷你", "Mini"),
    "win": ("窗口", "Window"),
    "follow": ("追问", "Ask"),
    "recent": ("最近图", "Recent"),
    "history": ("历史", "History"),
    "redo": ("重算", "Redo"),
    "cont": ("▶  连续截图", "▶  Watch"),
    "ready": ("就绪", "Ready"),
    "clear_cache": ("清空缓存", "Clear cache"),
    "reset_region": ("重置区域", "Reset area"),
    "selfcheck": ("自检", "Self-test"),
    "vision": ("视觉自检", "Vision test"),
    "update": ("检查更新", "Check update"),
    "balance": ("余额", "Balance"),
}

_LANG = {"v": "zh"}


def set_lang(value):
    _LANG["v"] = "en" if str(value).lower().startswith("en") else "zh"


def L(key):
    zh, en = LBL.get(key, (key, key))
    return en if _LANG["v"] == "en" else zh
