"""配置读写。"""
from __future__ import annotations

import json
import os
from pathlib import Path

def _app_dir() -> "Path":
    """打包成 exe 后，程序目录 = exe 所在目录；源码运行时 = 本文件目录。"""
    import sys as _sys

    if getattr(_sys, "frozen", False):
        return Path(_sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT = _app_dir()
CONFIG_PATH = ROOT / "config.json"
HISTORY_DIR = ROOT / "history"

DEFAULTS = {
    "api_key": "",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-chat",
    "samples": 3,
    "verify": True,
    "temperature": 0.7,
    "reader_order": ["winocr", "modlens"],
    "modlens_timeout": 45,
    "hotkey_region": "ctrl+alt+q",
    "hotkey_full": "ctrl+alt+w",
    "hotkey_redo": "ctrl+alt+r",
    "hotkey_paste": "ctrl+alt+v",
    "always_on_top": True,
    "font_size": 12,
    # 连续截图
    "interval": 3,
    "cont_bbox": None,          # [x, y, w, h] 绝对坐标；None = 整个屏幕
    "auto_copy": True,          # 出答案后自动复制到剪贴板
    # 准确度
    "ocr_quality": "high",      # high = 放大锐化 + 深色反色 + 低对比度二值化，多候选取最优；standard = 只跑原图（更快）
    "escalate": True,
    "reasoner": True,           # 采样答案有分歧时自动追加 2 次采样再投票
    "dedupe": True,             # 连续模式下「同一道题」不重复作答（省时间省调用）
    "cache": True,              # 同一道题第二次出现直接秒出（不再调模型）
    "multi": True,              # 一屏多题时自动切题、逐题作答
    "refocus": True,            # 答完把焦点还给原来那个窗口（考试时直接 Ctrl+V）
    "fast": False,              # 极速模式：只问 1 次、不审核、不追加
    # 提示
    "sound": True,              # 发现新题 / 出答案 / 出错时响一声
    # ---- v2.4 新增
    "auto_paste": False,        # 出答案后自动把答案粘进原来的输入框
    "auto_enter": False,        # 自动粘贴后再按一次回车（提交）
    "hotkey_next": "ctrl+alt+n",  # 下一题（一屏多题时切到下一题的答案）
    "dual": False,              # 双模型互检：chat 投票 + R1 独立解，一致才算高
    "vision_first": False,      # 几何图模式：直接用视觉模型读整图（不做本地 OCR 短路）
    "theme": "dark",            # dark / light（改完重新打开窗口生效）
    "font_scale": 100,          # 详情区字号百分比 80~160
    "opacity": 100,             # 窗口不透明度 60~100
    "remember_pos": False,      # 记住窗口最后的位置
    "last_pos": "",             # "WxH+X+Y"
    "check_update": True,       # 启动时检查 GitHub 有没有新版本
    "tray": True,               # 关窗口时缩到托盘（而不是退出）
    "autostart": False,
    # v2.6：纯文字作答 / 窗口截图 / 迷你浮窗 / 朗读 / 剪贴板监听 / 函数图像 / 考试日期
    "hotkey_text": "ctrl+alt+t",
    "hotkey_mini": "ctrl+alt+m",
    "watch_clip": False,
    "watch_titles": "",
    "tts": False,
    "tts_lang": "zh",
    "mini": False,
    "plot": True,
    "exam_date": "",
         # 开机自启
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.is_file():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cfg.update({k: v for k, v in raw.items() if k in DEFAULTS})
        except Exception:  # noqa: BLE001
            pass
    # 布尔字段一律归一（避免 BooleanVar 的 tristate None 写进配置）
    for _k in ("verify", "escalate", "auto_copy", "always_on_top", "sound",
               "dedupe", "cache", "multi", "fast", "reasoner", "refocus",
               "auto_paste", "auto_enter", "dual", "vision_first",
               "remember_pos", "check_update", "tray", "autostart",
               "watch_clip", "tts", "mini", "plot"):
        if _k in cfg:
            cfg[_k] = bool(cfg.get(_k, DEFAULTS[_k]))
    # 环境变量优先
    env_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if env_key and not cfg.get("api_key"):
        cfg["api_key"] = env_key
    return cfg


def save(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS}
    CONFIG_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def save_history(question: str, answer: str, meta: str = "") -> Path:
    """把一次问答存档，方便回看。"""
    import time

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = HISTORY_DIR / f"{stamp}.md"
    body = f"# {stamp}\n\n{meta}\n\n## 题目（识别文本）\n\n{question}\n\n## 答案\n\n{answer}\n"
    path.write_text(body, encoding="utf-8")
    return path


RECORDS_PATH = ROOT / "records.jsonl"
ERRORS_PATH = ROOT / "errors.json"


def add_record(rec: dict) -> None:
    """给「历史面板 / 导出 CSV」攒一条记录（jsonl，一行一条）。"""
    import time

    try:
        rec = dict(rec)
        rec.setdefault("ts", time.strftime("%Y-%m-%d %H:%M:%S"))
        with open(RECORDS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def load_records(limit: int = 500) -> list:
    out: list = []
    try:
        if RECORDS_PATH.is_file():
            for line in RECORDS_PATH.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:  # noqa: BLE001
                    continue
    except Exception:  # noqa: BLE001
        pass
    return out[-limit:]


def export_records_csv(path) -> int:
    """导出做题记录为 CSV（UTF-8-SIG，Excel 直接打开不乱码）。"""
    import csv

    recs = load_records(limit=100000)
    cols = ["ts", "title", "answer", "fill", "confidence", "model",
            "elapsed", "calls", "backend", "cached"]
    try:
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in recs:
                w.writerow({c: r.get(c, "") for c in cols})
    except Exception:  # noqa: BLE001
        return 0
    return len(recs)


def load_errors() -> dict:
    try:
        if ERRORS_PATH.is_file():
            data = json.loads(ERRORS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:  # noqa: BLE001
        pass
    return {}


def add_error(key: str, text: str, answer: str) -> int:
    """记一道「模型答错了」的题：下次遇到同一题会自动多问几路。"""
    import time

    data = load_errors()
    data[key] = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "text": (text or "")[:2000], "answer": answer or "",
                 "hits": int((data.get(key) or {}).get("hits", 0)) + 1}
    try:
        ERRORS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                               encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return len(data)
