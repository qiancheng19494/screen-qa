"""配置读写。"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
HISTORY_DIR = ROOT / "history"

DEFAULTS = {
    "api_key": "",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-chat",
    "samples": 3,
    "verify": True,
    "temperature": 0.7,
    "reader_order": ["modlens", "winocr"],
    "modlens_timeout": 300,
    "hotkey_region": "ctrl+alt+q",
    "hotkey_full": "ctrl+alt+w",
    "always_on_top": True,
    "font_size": 12,
    # 连续截图
    "interval": 3,
    "cont_bbox": None,          # [x, y, w, h] 绝对坐标；None = 整个屏幕
    "auto_copy": True,          # 出答案后自动复制到剪贴板
    # 准确度
    "ocr_quality": "high",      # high = 放大锐化 + 深色反色多候选取最优；standard = 只跑原图（更快）
    "escalate": True,           # 采样答案有分歧时自动追加 2 次采样再投票
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
