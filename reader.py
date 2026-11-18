"""图片 → 文字 的读取层（可插拔）。

支持两种后端：
  * modlens  —— 视觉大模型桥（用户指定；复用本机已登录的 codex/claude CLI）
  * winocr   —— Windows 系统自带 OCR（快、离线、免费，不需要装任何东西）

默认顺序：先 modlens 视觉模型，失败/超时自动降级到本地 OCR，
并把降级原因显示出来（不静默失败）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MODLENS_LOCAL = Path(
    os.path.expanduser("~")
) / ".dsh/profiles/desktop/node_modules/@liustack/modlens/dist/main.js"


@dataclass
class ReadResult:
    text: str = ""
    backend: str = ""
    summary: str = ""
    uncertainty: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    attempts: list = field(default_factory=list)
    elapsed: float = 0.0
    error: str = ""


# --------------------------------------------------------------- modlens
def _modlens_cmd() -> Optional[list[str]]:
    """找一个可用的 modlens 调用方式。"""
    exe = shutil.which("modlens")
    if exe:
        return [exe]
    node = shutil.which("node")
    if node and MODLENS_LOCAL.is_file():
        return [node, str(MODLENS_LOCAL)]
    npx = shutil.which("npx")
    if npx:
        return [npx, "--yes", "--package", "@liustack/modlens@3.27.5", "modlens"]
    return None


def read_with_modlens(image_path: str, timeout_s: int = 300,
                      prompt: str = "", provider: str = "") -> ReadResult:
    """用 modlens 视觉桥读取图片。"""
    t0 = time.time()
    res = ReadResult(backend="modlens")
    cmd = _modlens_cmd()
    if not cmd:
        res.error = "找不到 modlens（需要 node 或 npx，以及 @liustack/modlens）"
        return res

    out_path = Path(tempfile.gettempdir()) / f"modlens_{int(time.time()*1000)}.json"
    args = cmd + ["-i", image_path, "--timeout", str(max(30, timeout_s) * 1000),
                  "-o", str(out_path)]
    if prompt:
        args += ["--prompt", prompt]
    if provider:
        args += ["-p", provider]

    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout_s + 40,
            encoding="utf-8", errors="replace",
        )
        stderr = (proc.stderr or "").strip()
        if not out_path.is_file():
            res.error = (stderr[-400:] or f"modlens 退出码 {proc.returncode}")
            res.elapsed = time.time() - t0
            return res

        raw = json.loads(out_path.read_text(encoding="utf-8"))
        result = raw.get("result") or {}
        meta = raw.get("meta") or {}
        ocr = result.get("ocr") or {}
        res.summary = (result.get("summary") or "").strip()
        res.text = (ocr.get("full_text") or "").strip()
        if not res.text:
            parts = []
            for region in (result.get("layout") or {}).get("regions") or []:
                t = (region.get("text") or "").strip()
                if t:
                    parts.append(t)
            res.text = "\n".join(parts).strip()
        res.uncertainty = result.get("uncertainty") or []
        res.warnings = meta.get("warnings") or []
        res.attempts = meta.get("attempts") or []
        if not res.text and res.summary:
            res.text = res.summary
        if not res.text:
            res.error = "modlens 返回了空文本"
    except subprocess.TimeoutExpired:
        res.error = f"modlens 超时（>{timeout_s}s）"
    except json.JSONDecodeError as exc:
        res.error = f"modlens 输出不是合法 JSON: {exc}"
    except Exception as exc:  # noqa: BLE001
        res.error = f"modlens 调用失败: {exc}"
    finally:
        try:
            out_path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

    res.elapsed = time.time() - t0
    return res


# --------------------------------------------------------------- 本地 OCR
def read_with_ocr(image_path: str, timeout_s: int = 120,
                  quality: str = "standard") -> ReadResult:
    t0 = time.time()
    res = ReadResult(backend="winocr")
    try:
        import psocr

        res.text = psocr.recognize(
            image_path, timeout_s=timeout_s, quality=quality).strip()
        if not res.text:
            res.error = "OCR 没识别到文字（图片里可能没有文本）"
    except Exception as exc:  # noqa: BLE001
        res.error = f"OCR 失败: {exc}"
    res.elapsed = time.time() - t0
    return res


# --------------------------------------------------------------- 统一入口
def read_image(image_path: str, order: list[str] | None = None,
               modlens_timeout: int = 300, log=None,
               ocr_quality: str = "standard") -> ReadResult:
    """按顺序尝试各后端，第一个成功的返回。

    order 默认 ["modlens", "winocr"]；["winocr"] 可跳过视觉模型（快）。
    ocr_quality="high" 时本地 OCR 会多试放大锐化/反色候选，慢约 1 秒但更准。
    """
    log = log or (lambda m: None)
    order = order or ["modlens", "winocr"]
    errors: list[str] = []

    for backend in order:
        if backend == "modlens":
            log("视觉模型识别中（首次可能要 1-3 分钟）…")
            r = read_with_modlens(image_path, timeout_s=modlens_timeout)
        elif backend in ("winocr", "psocr", "rapidocr"):
            log("本地 OCR 识别中…")
            r = read_with_ocr(image_path, quality=ocr_quality)
        else:
            continue

        if r.text:
            if errors:
                r.warnings = list(errors) + list(r.warnings)
            return r
        errors.append(f"{backend} 不可用（{r.error}）")

    out = ReadResult(backend="none")
    out.error = "所有识别方式都失败了 —— " + "；".join(errors)
    return out


def backend_status() -> dict:
    """各后端可用性（给界面显示）。"""
    status = {}
    cmd = _modlens_cmd()
    status["modlens"] = {
        "available": cmd is not None,
        "detail": "已找到 modlens" if cmd else "未找到 modlens / node",
    }
    try:
        import psocr

        status["winocr"] = {
            "available": psocr.available(),
            "detail": "Windows 系统 OCR 可用" if psocr.available() else "找不到 powershell.exe",
        }
    except Exception as exc:  # noqa: BLE001
        status["winocr"] = {"available": False, "detail": str(exc)}
    return status


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python reader.py <图片路径> [--ocr-only]")
        raise SystemExit(2)
    order = ["winocr"] if "--ocr-only" in sys.argv else None
    r = read_image(sys.argv[1], order=order, log=lambda m: print(m, file=sys.stderr))
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(f"backend={r.backend} elapsed={r.elapsed:.2f}s")
    for w in r.warnings:
        print(f"[warn] {w}", file=sys.stderr)
    print(r.text or f"[error] {r.error}")
