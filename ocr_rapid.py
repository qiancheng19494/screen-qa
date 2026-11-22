"""RapidOCR 后端：离线、对公式/符号行更稳（WinRT OCR 会整行吞掉）。

对比实测（同一批题图）：
  c10 `(x^2 - 9)/(x - 3)`  WinRT: 整行丢失   | RapidOCR: `(x~2-9) / (x - 3)`
  c18 `C(5, 2)`            WinRT: 整行丢失   | RapidOCR: `C(5, 2)`
  c02 `sqrt(2x + 1) = 5`   WinRT: `sqrt()x…` | RapidOCR: `sqrt(2x + 1) = 5`（系数 2 保住了）

引擎只初始化一次（约 2 秒，之后每张 1–2 秒）。
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

_ENGINE = None
_LOCK = threading.Lock()
_ERROR = ""


def available() -> bool:
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def engine_info() -> str:
    if not available():
        return "未安装（pip install rapidocr-onnxruntime）"
    if _ERROR:
        return f"初始化失败：{_ERROR}"
    return "RapidOCR（PP-OCR，离线）"


def _engine():
    global _ENGINE, _ERROR
    if _ENGINE is not None:
        return _ENGINE
    with _LOCK:
        if _ENGINE is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
                _ENGINE = RapidOCR()
            except Exception as exc:  # noqa: BLE001
                _ERROR = str(exc)
                raise
    return _ENGINE


def warmup() -> bool:
    """后台预热，避免第一次框选时白等 2 秒。"""
    try:
        _engine()
        return True
    except Exception:  # noqa: BLE001
        return False


def recognize(path: str | Path, min_side: int = 0, code_mode: bool = False) -> str:
    """返回按行拼接的文本；失败抛异常由上层兜底。"""
    eng = _engine()
    result, _elapse = eng(str(path))
    if not result:
        return ""
    rows = []
    for item in result:
        try:
            box, text, score = item[0], item[1], (item[2] if len(item) > 2 else 1.0)
        except Exception:  # noqa: BLE001
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        rows.append((min(ys), min(xs), str(text)))
    rows.sort(key=lambda r: r[0])
    if not code_mode or not rows:
        return "\n".join(t for _y, _x, t in rows).strip()
    # 代码模式：按行首 x 坐标还原缩进层级（RapidOCR 会丢缩进，代码题必须补回来）
    base = min(r[1] for r in rows)
    heights = [abs(rows[i + 1][0] - rows[i][0]) for i in range(len(rows) - 1)] or [20]
    char_w = max(6.0, sorted(heights)[len(heights) // 2] * 0.42)
    out = []
    for _y, x, t in rows:
        level = int(round((x - base) / (char_w * 4)))
        out.append("    " * max(0, min(level, 6)) + t)
    return "\n".join(out).strip()
