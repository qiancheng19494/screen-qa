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

from PIL import Image
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


_CACHE = {}
_CACHE_MAX = 24


def _fp(path: str, code_mode: bool):
    import hashlib
    import os
    try:
        st = os.stat(path)
        raw = f"{os.path.abspath(path)}|{st.st_mtime_ns}|{st.st_size}|{int(code_mode)}"
    except OSError:
        return None
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def recognize(path: str | Path, min_side: int = 0, code_mode: bool = False) -> str:
    """识别一张图，返回按行拼接的文本；同一文件（含 code_mode）命中缓存直接复用。"""
    key = _fp(str(path), bool(code_mode))
    if key and key in _CACHE:
        return _CACHE[key]
    eng = _engine()
    if eng is None:
        raise RuntimeError("RapidOCR 不可用")
    img = Image.open(str(path)).convert("RGB")
    if min_side and min(img.size) < min_side:
        k = min_side / float(min(img.size))
        img = img.resize((int(img.width * k), int(img.height * k)), Image.LANCZOS)
    import numpy as np
    arr = np.asarray(img)[:, :, ::-1]  # RGB -> BGR
    raw = eng(arr)
    items = raw[0] if isinstance(raw, tuple) else raw
    rows = []
    for it in items or []:
        try:
            box, txt, _score = it[0], it[1], it[2] if len(it) > 2 else 0.0
        except Exception:  # noqa: BLE001
            continue
        ys = [float(pt[1]) for pt in box]
        xs = [float(pt[0]) for pt in box]
        if str(txt).strip():
            rows.append((min(ys), min(xs), max(ys) - min(ys), str(txt)))
    rows.sort(key=lambda r: (r[0], r[1]))
    if code_mode and rows:
        base = min(r[1] for r in rows)
        heights = sorted(r[2] for r in rows) or [16.0]
        char_w = max(6.0, heights[len(heights) // 2] * 0.42)
        out_lines = []
        for _y, x, _h, txt in rows:
            level = int(round((x - base) / (char_w * 4)))
            out_lines.append("    " * max(0, min(6, level)) + txt)
        text = "\n".join(out_lines)
    else:
        text = "\n".join(r[3] for r in rows)
    if key and text:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = text
    return text
