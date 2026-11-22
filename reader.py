"""图片 → 文字 的读取层（可插拔）。

支持两种后端：
  * modlens  —— 视觉大模型桥（用户指定；复用本机已登录的 codex/claude CLI）
  * winocr   —— Windows 系统自带 OCR（快、离线、免费，不需要装任何东西）

默认顺序：先 modlens 视觉模型，失败/超时自动降级到本地 OCR，
并把降级原因显示出来（不静默失败）。
"""
from __future__ import annotations

import re

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

def _app_dir() -> "Path":
    """打包成 exe 后，程序目录 = exe 所在目录；源码运行时 = 本文件目录。"""
    import sys as _sys

    if getattr(_sys, "frozen", False):
        return Path(_sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT = _app_dir()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MODLENS_LOCAL = Path(
    os.path.expanduser("~")
) / ".dsh/profiles/desktop/node_modules/@liustack/modlens/dist/main.js"

# modlens 需要 spawn 本机的 codex / claude CLI；而它们在 Windows 上是 npm 的
# .cmd 批处理垫片，Node 直接 spawn .cmd 会被系统拒绝（EINVAL/EPERM）。
# tools/cli_shims 里是两个真 .exe 垫片（内容只是转发到对应 CLI），
# 把该目录放到 PATH 最前面，modlens 就会先命中 .EXE，从而能正常启动。
SHIM_DIR = ROOT / "tools" / "cli_shims"


def ensure_shims() -> bool:
    """把 cli_shims 前置进 PATH（幂等）。返回是否真的添加了。"""
    try:
        if not SHIM_DIR.is_dir():
            return False
        if not any((SHIM_DIR / n).is_file() for n in ("codex.exe", "claude.exe")):
            return False
        cur = os.environ.get("PATH", "")
        head = str(SHIM_DIR)
        if cur.split(os.pathsep)[0] == head:
            return False
        os.environ["PATH"] = head + os.pathsep + cur
        return True
    except Exception:
        return False


ensure_shims()


def _text_score(text: str) -> float:
    """给一段文本打粗糙的质量分（0-100），用于决定要不要上视觉模型。"""
    try:
        import psocr

        return float(psocr.text_score(text))
    except Exception:  # noqa: BLE001
        t = "".join(text.split())
        if not t:
            return 0.0
        ok = sum(1 for c in t if c.isalnum() or c in "=+-−×÷/*^().,%:：；[]{}<>√")
        return round(100.0 * ok / len(t), 1)


# 本地 OCR 质量分达到这个值就不再叫视觉模型（0-100，经验值）

_EMPTY_PAREN = re.compile(r"\bsqrt\s*\(\s*\)|\(\s*\)", re.I)
VISION_FALLBACK_SCORE = 45.0


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
    if not modlens_usable():
        res.error = "视觉链路在本进程里已判定不可用（前面失败过），本次跳过"
        return res
    cmd = _modlens_cmd()
    if not cmd:
        res.error = "找不到 modlens（需要 node 或 npx，以及 @liustack/modlens）"
        _mark_modlens_dead(res.error)
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
            if res.error and ("spawn" in res.error.lower() or "EPERM" in res.error
                              or "Every configured vision provider failed" in res.error):
                _mark_modlens_dead(res.error)
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
    if res.error and ("spawn" in res.error.lower() or "EPERM" in res.error
                      or "找不到" in res.error or "Every configured vision provider failed" in res.error):
        _mark_modlens_dead(res.error)
    return res


# --------------------------------------------------------------- 本地 OCR
def read_with_ocr(image_path: str, timeout_s: int = 120,
                  quality: str = "standard") -> ReadResult:
    t0 = time.time()
    res = ReadResult(backend="winocr")
    try:
        import ocrfix
        import psocr

        raw = psocr.recognize(
            image_path, timeout_s=timeout_s, quality=quality).strip()
        # 空括号 = 括号里的系数被吞（实测 sqrt(2x+1)=5 会被读成 sqrt()x + 1 ) = 5）
        if _EMPTY_PAREN.search(raw):
            res.warnings.append(
                "OCR 读到了空括号/缺系数（如 sqrt()x）——括号里或根号下的系数可能被吞了；"
                "建议把题干那一行一起框进来，或按 Ctrl+Alt+T 用剪贴板文字作答")
        res.text, fixes = ocrfix.repair(raw)
        if fixes:
            res.warnings.append(f"修了 {fixes} 处 OCR 形近字/数学符号误识别")
        if not res.text:
            res.error = "OCR 没识别到文字（图片里可能没有文本）"
    except Exception as exc:  # noqa: BLE001
        res.error = f"OCR 失败: {exc}"
    res.elapsed = time.time() - t0
    return res


# --------------------------------------------------------------- 统一入口
def _norm_compare(text: str) -> str:
    import re as _re
    return _re.sub(r"[\s\u3000]+", "", text or "").lower()


def _ratio(a: str, b: str) -> float:
    import difflib
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


_MODLENS_DEAD = False   # 本进程里视觉链路的"永久失败"标记（如沙箱禁 spawn）


def _mark_modlens_dead(err: str) -> None:
    global _MODLENS_DEAD
    _MODLENS_DEAD = True


def modlens_usable() -> bool:
    return not _MODLENS_DEAD


def cross_check_text(image_path: str, ocr_text: str, timeout_s: int = 45,
                     log=None) -> tuple[str, list[str]]:
    """双读交叉校验：让视觉模型也读一遍，和本地 OCR 比。

    返回 (要采用的文本, 警告列表)。阈值故意保守——只有两边差得明显才警告，
    否则正常题每次都提示"读法有分歧"反而吵人。
    """
    log = log or (lambda m: None)
    warns: list[str] = []
    if not modlens_usable():
        return ocr_text, warns
    try:
        vr = read_with_modlens(image_path, timeout_s=timeout_s)
    except Exception as exc:  # noqa: BLE001
        return ocr_text, warns
    if not vr.text:
        return ocr_text, warns
    ratio = _ratio(_norm_compare(ocr_text), _norm_compare(vr.text))
    if ratio < 0.55:
        warns.append(
            f"⚠ OCR 与视觉模型读到的题面差异较大（相似度 {ratio:.0%}），"
            f"请核对是不是被读错了：\n[本地 OCR] {ocr_text.strip()[:160]}\n"
            f"[视觉模型] {vr.text.strip()[:160]}")
        log(f"双读不一致（相似度 {ratio:.0%}），已在提示里标出两版原文")
    else:
        log(f"双读一致（相似度 {ratio:.0%}）")
    # 采用字数更多的那个（少字往往就是被吞掉的符号/整行）
    if len(_norm_compare(vr.text)) > len(_norm_compare(ocr_text)) * 1.15:
        return vr.text, warns
    return ocr_text, warns


def read_image(image_path: str, order: list[str] | None = None,
               modlens_timeout: int = 300, log=None,
               ocr_quality: str = "standard",
               cross_check: bool = False) -> ReadResult:
    """按顺序尝试各后端，第一个成功的返回。

    order 默认 ["modlens", "winocr"]；["winocr"] 可跳过视觉模型（快）。
    ocr_quality="high" 时本地 OCR 会多试放大锐化/反色候选，慢约 1 秒但更准。
    """
    log = log or (lambda m: None)
    order = order or ["winocr", "modlens"]
    errors: list[str] = []
    ocr_first = None  # 本地 OCR 的保底结果：视觉模型也不可用时，至少把它交上去

    # ── 智能短路：本地 OCR 先跑（0.5 秒级），质量够好就不去碰视觉模型。
    # 视觉模型要拉起 CLI，冷启动可能几十秒；只有 OCR 明显不行（乱码/字太少）
    # 或这题必须看图（几何/图表）时才值得等。
    if order and order[0] in ("winocr", "psocr", "rapidocr"):
        log("本地 OCR 识别中…")
        first = read_with_ocr(image_path, quality=ocr_quality)
        if first.text:
            try:
                import ocrfix

                fixed, n = ocrfix.repair(first.text)
                if n:
                    first.text = fixed
                    first.warnings = list(first.warnings) + [f"修了 {n} 处 OCR 形近字/数学符号误识别"]
            except Exception:  # noqa: BLE001
                pass
            score = _text_score(first.text)
            if score >= VISION_FALLBACK_SCORE and len(first.text.strip()) >= 25:
                log(f"本地 OCR 够用（质量 {score:.0f}），跳过视觉模型")
                risky = ""
                try:
                    import ocrcheck
                    risky = ocrcheck.risk(first.text) or ""
                except Exception:  # noqa: BLE001
                    risky = ""
                # v2.13: 读到可疑内容（空括号/无符号行）且视觉可用时，自动双读一次
                if risky and not cross_check and modlens_usable():
                    log(f"OCR 读到可疑内容（{risky}）→ 自动双读校验一次")
                    cross_check = True
                if cross_check:
                    txt, warns = cross_check_text(
                        image_path, first.text, timeout_s=min(modlens_timeout, 20),
                        log=log)
                    if txt != first.text:
                        first.text = txt
                        first.warnings = list(first.warnings) + ["双读交叉校验：采用了视觉模型版本"]
                    if warns:
                        first.warnings = list(first.warnings) + warns
                return first
            log(f"本地 OCR 质量偏低（{score:.0f}），再用视觉模型看一次…")
            errors.append(f"winocr 质量偏低（{score:.0f}）")
            ocr_first = first
        else:
            errors.append(f"winocr 不可用（{first.error}）")
        order = [b for b in order if b not in ("winocr", "psocr", "rapidocr")]

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

    # 视觉模型也不可用时，别把已经读到的文字扔掉：标注质量偏低照样往上送，
    # 让模型自己纠正（这比"什么都没识别到"对用户有用得多）。
    if ocr_first is not None and ocr_first.text.strip():
        ocr_first.warnings = list(ocr_first.warnings) + [
            "本地 OCR 质量偏低，视觉模型也不可用；结果可能不准，建议重框或放大"]
        return ocr_first

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
