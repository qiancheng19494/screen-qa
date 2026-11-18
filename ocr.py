"""屏幕文字识别（OCR）模块。

优先使用 Windows 系统自带的 OCR 引擎（Windows.Media.Ocr，通过 winsdk 调用）：
  - 离线、免费、无需下载模型
  - 中英文识别质量好，对屏幕截图这种干净文字特别合适
如果 winsdk 不可用，则回退到 RapidOCR（onnxruntime 版）。
"""
from __future__ import annotations

import asyncio
import io

from PIL import Image, ImageOps

_winocr_engine = None
_winocr_error: str | None = None


class OcrError(RuntimeError):
    pass


# ---------------------------------------------------------------- 预处理
def preprocess(img: Image.Image, min_side: int = 1400) -> Image.Image:
    """对截图做轻度增强：小图放大 + 灰度 + 自动对比度。

    屏幕截图本身很干净，过度二值化反而会让细笔画（尤其中文）丢失，
    所以这里只做"放大 + 对比度拉伸"两件事。
    """
    img = img.convert("RGB")
    w, h = img.size
    short = min(w, h)
    if short > 0 and short < min_side:
        scale = min(3.0, min_side / short)
        if scale > 1.05:
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    gray = ImageOps.grayscale(img)
    gray = ImageOps.autocontrast(gray, cutoff=1)
    return gray


# ---------------------------------------------------------------- Windows OCR
def _get_winocr_engine():
    """创建（并缓存）Windows OCR 引擎，语言优先级：简体中文 → 英文 → 任意可用。"""
    global _winocr_engine, _winocr_error
    if _winocr_engine is not None:
        return _winocr_engine
    if _winocr_error is not None:
        raise OcrError(_winocr_error)

    try:
        from winsdk.windows.media.ocr import OcrEngine
    except Exception as exc:  # noqa: BLE001
        _winocr_error = f"winsdk 不可用: {exc}"
        raise OcrError(_winocr_error) from exc

    langs = []
    try:
        for lang in OcrEngine.available_recognizer_languages:
            langs.append(lang)
    except Exception:  # noqa: BLE001
        pass

    if not langs:
        _winocr_error = (
            "Windows 未安装任何 OCR 语言包。请到「设置 → 时间和语言 → 语言」"
            "添加中文(简体)或英语的「光学字符识别」可选功能。"
        )
        raise OcrError(_winocr_error)

    def score(lang) -> int:
        tag = (lang.language_tag or "").lower()
        if tag.startswith("zh-hans") or tag.startswith("zh-cn"):
            return 0
        if tag.startswith("en"):
            return 1
        if tag.startswith("zh"):
            return 2
        return 9

    langs.sort(key=score)
    try:
        _winocr_engine = OcrEngine.try_create_from_language(langs[0])
    except Exception as exc:  # noqa: BLE001
        _winocr_error = f"创建 OCR 引擎失败: {exc}"
        raise OcrError(_winocr_error) from exc

    if _winocr_engine is None:
        _winocr_error = "创建 Windows OCR 引擎失败（语言包不可用）。"
        raise OcrError(_winocr_error)
    return _winocr_engine


def _join_line(words) -> str:
    """把一行里的 word 拼成文本：按间距决定是否补空格。"""
    if not words:
        return ""
    parts: list[str] = []
    prev = None
    for widx in range(len(words)):
        w = words[widx]
        text = w.text or ""
        if not text:
            continue
        rect = w.bounding_rect
        if prev is not None:
            prev_rect = prev.bounding_rect
            gap = rect.x - (prev_rect.x + prev_rect.width)
            h = max(prev_rect.height, rect.height) or 1.0
            # 间距超过约 0.35 个字高 → 认为中间有空格
            if gap > 0.35 * h:
                # 中文之间加空格反而难看，只在两侧含拉丁字符时补空格
                a = prev.text[-1:] if prev.text else ""
                b = text[:1]
                if _is_latin(a) or _is_latin(b):
                    parts.append(" ")
        parts.append(text)
        prev = w

    # 中文场景下引擎常把每个汉字拆成独立 word，逐字之间不应有空格
    out = "".join(parts)
    return out


def _is_latin(ch: str) -> bool:
    return bool(ch) and ("a" <= ch.lower() <= "z" or ch.isdigit())


async def _recognize_winocr_async(img: Image.Image) -> str:
    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data = buf.getvalue()

    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(data)
    await writer.store_async()
    await writer.flush_async()
    writer.detach_stream()
    stream.seek(0)

    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()

    engine = _get_winocr_engine()
    result = await engine.recognize_async(bitmap)

    lines = []
    for line in result.lines:
        text = _join_line(list(line.words)).strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


# ---------------------------------------------------------------- RapidOCR 回退
def _recognize_rapidocr(img: Image.Image) -> str:
    try:
        from rapidocr_onnxruntime import RapidOCR
    except Exception as exc:  # noqa: BLE001
        raise OcrError(
            "没有可用的 OCR 引擎：Windows OCR(winsdk) 与 RapidOCR 都不可用。"
            "请先运行: python -m pip install winsdk"
        ) from exc

    import numpy as np

    engine = RapidOCR()
    arr = np.array(img.convert("RGB"))
    result, _ = engine(arr)
    if not result:
        return ""
    # result: [[box, text, score], ...]，按垂直位置排序
    items = []
    for box, text, _score in result:
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        items.append((sum(ys) / len(ys), sum(xs) / len(xs), text))
    items.sort(key=lambda t: (round(t[0] / 12), t[1]))
    return "\n".join(t[2] for t in items)


# ---------------------------------------------------------------- 对外接口
def recognize(image: Image.Image, engine: str = "auto") -> str:
    """识别一张 PIL 图片里的文字，返回按行拼接的纯文本。"""
    if image is None:
        raise OcrError("图片为空")
    img = preprocess(image)

    if engine in ("auto", "windows"):
        try:
            return asyncio.run(_recognize_winocr_async(img))
        except OcrError:
            if engine == "windows":
                raise
        except Exception as exc:  # noqa: BLE001
            if engine == "windows":
                raise OcrError(f"Windows OCR 失败: {exc}") from exc

    return _recognize_rapidocr(img)


def engine_info() -> str:
    """当前可用的 OCR 引擎描述，给界面/日志用。"""
    try:
        from winsdk.windows.media.ocr import OcrEngine

        langs = [l.language_tag for l in OcrEngine.available_recognizer_languages]
        return f"Windows OCR 可用，语言包: {', '.join(langs) or '无'}"
    except Exception as exc:  # noqa: BLE001
        return f"Windows OCR 不可用（{exc}）"
