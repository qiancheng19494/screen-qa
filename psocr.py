"""Windows 系统 OCR（通过 PowerShell 调 Windows.Media.Ocr）。

不需要 pip 安装任何东西 —— Windows 10/11 自带 OCR 引擎（中英文都认）。
小字/模糊时可以开 enhance（放大 + 灰度 + 对比度增强），但会损失部分标点，
所以默认用原图。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PS1 = ROOT / "psocr.ps1"


def _powershell() -> str | None:
    for cand in ("powershell.exe", "pwsh.exe"):
        p = shutil.which(cand)
        if p:
            return p
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    p = os.path.join(sysroot, r"System32\WindowsPowerShell\v1.0\powershell.exe")
    return p if os.path.isfile(p) else None


def preprocess(img_path: str, min_side: int = 1600) -> str:
    """放大 + 灰度 + 对比度，返回新文件路径。"""
    from PIL import Image, ImageOps

    img = Image.open(img_path)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    w, h = img.size
    short = min(w, h)
    if short < min_side:
        scale = min(3.0, min_side / max(1, short))
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    if img.mode == "RGB":
        img = img.convert("L")
    img = ImageOps.autocontrast(img, cutoff=1)
    out = Path(tempfile.gettempdir()) / f"psocr_in_{int(time.time()*1000)}.png"
    img.save(out)
    return str(out)


_GOOD_RE = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")
_JUNK_RE = re.compile(r"[^\s0-9A-Za-z\u4e00-\u9fff，。、；：？！（）()\[\]{}.,;:!?%+\-*/=<>^_'\"|~·…—“”‘’《》]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_WORD_RE = re.compile(r"[A-Za-z]{2,}")


def text_score(text: str) -> float:
    """粗估一段 OCR 结果的质量：有效字符越多、乱码越少越好。

    用于从多个候选（原图 / 放大锐化 / 反色）里挑最可信的那份。
    """
    if not text:
        return -1e9
    good = len(_GOOD_RE.findall(text))
    junk = len(_JUNK_RE.findall(text))
    words = len(_CJK_RE.findall(text)) + len(_WORD_RE.findall(text))
    lines = len([ln for ln in text.splitlines() if ln.strip()])
    return good + words * 0.5 + min(lines, 12) * 0.5 - junk * 2.5


def _mean_luma(img) -> float:
    from PIL import ImageStat

    return float(ImageStat.Stat(img.convert("L")).mean[0])


def build_variants(image_path: str, quality: str = "standard") -> list[tuple[str, "object"]]:
    """按质量档位生成待识别的候选图（质量越高试的越多、越慢）。"""
    from PIL import Image, ImageFilter, ImageOps

    img = Image.open(image_path)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    out: list[tuple[str, object]] = [("原图", img)]
    if quality == "high":
        w, h = img.size
        short = min(w, h)
        # 只对「截取的小区域」做放大锐化：大图（整屏）已经够清晰，再放大会白等 2 秒
        if short < 1400:
            scale = min(2.5, 1600 / max(1, short))
            big = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            out.append(("放大锐化", big.filter(
                ImageFilter.UnsharpMask(radius=2, percent=140, threshold=3))))
        if _mean_luma(img) < 110:  # 深色底 → 反色成浅底，Windows OCR 更擅长
            out.append(("反色", ImageOps.invert(img.convert("RGB"))))
    return out


def _run(ps: str, path: str, timeout_s: int) -> str:
    out_file = Path(tempfile.gettempdir()) / f"psocr_out_{int(time.time()*1000)}.txt"
    try:
        proc = subprocess.run(
            [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(PS1), "-Path", path, "-OutFile", str(out_file)],
            capture_output=True, timeout=timeout_s,
        )
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()[-400:]
            raise RuntimeError(f"PowerShell OCR 失败: {err or proc.returncode}")
        if not out_file.is_file():
            raise RuntimeError("PowerShell OCR 没有产出文本")
        return out_file.read_text(encoding="utf-8-sig").strip()
    finally:
        try:
            out_file.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


def recognize(image_path: str, timeout_s: int = 120, enhance: bool | None = None,
              quality: str = "standard") -> str:
    """识别图片文字。

    quality="high" 时会额外试「放大锐化」「深色反色」，用 text_score 挑最好的一份；
    quality="standard" 只在结果太短时才补救一次（快，约 0.6 秒）。
    """
    ps = _powershell()
    if not ps:
        raise RuntimeError("找不到 powershell.exe")

    q = "high" if (enhance is True or quality == "high") else "standard"
    best_text, best_score = "", -1e9
    tmp_files: list[str] = []

    try:
        for name, img in build_variants(image_path, q):
            target = image_path
            if name != "原图":
                tmp = Path(tempfile.gettempdir()) / f"psocr_var_{int(time.time()*1000)}_{name}.png"
                img.save(tmp)
                tmp_files.append(str(tmp))
                target = str(tmp)
            try:
                text = _run(ps, target, timeout_s)
            except Exception:  # noqa: BLE001
                continue
            sc = text_score(text)
            if sc > best_score:
                best_text, best_score = text, sc
            # 标准档：原图就够用就别折腾
            if q == "standard" and name == "原图" and len(
                    _GOOD_RE.findall(text)) >= 12 and enhance is not True:
                break
    finally:
        for f in tmp_files:
            try:
                os.unlink(f)
            except Exception:  # noqa: BLE001
                pass

    if enhance is True and not best_text.strip():
        # 极端情况：所有候选都空，退回老的灰度增强路径
        tmp = None
        try:
            tmp = preprocess(image_path)
            best_text = _run(ps, tmp, timeout_s) or best_text
        except Exception:  # noqa: BLE001
            pass
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except Exception:  # noqa: BLE001
                    pass
    return best_text


def available() -> bool:
    return _powershell() is not None and PS1.is_file()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python psocr.py <图片路径> [--enhance]")
        raise SystemExit(2)
    t0 = time.time()
    text = recognize(sys.argv[1], enhance=("--enhance" in sys.argv))
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(text)
    print(f"\n[{time.time()-t0:.1f}s]", file=sys.stderr)
