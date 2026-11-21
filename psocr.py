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
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

def _app_dir() -> "Path":
    """打包成 exe 后，程序目录 = exe 所在目录；源码运行时 = 本文件目录。"""
    import sys as _sys

    if getattr(_sys, "frozen", False):
        return Path(_sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT = _app_dir()
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


def _contrast(img) -> float:
    """灰度标准差，越小说明字和底越接近（低对比度页面/半透明浮层）。"""
    from PIL import ImageStat

    return float(ImageStat.Stat(img.convert("L")).stddev[0])


def _otsu_threshold(hist: list[int], total: int) -> int:
    """经典 Otsu：找一个让类间方差最大的阈值。"""
    sum_all = sum(i * hist[i] for i in range(256))
    sum_b = 0.0
    w_b = 0
    best, best_var = 128, -1.0
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        m_b = sum_b / w_b
        m_f = (sum_all - sum_b) / w_f
        var = w_b * w_f * (m_b - m_f) ** 2
        if var > best_var:
            best_var, best = var, t
    return best


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
        if _contrast(img) < 25:  # 真正的低对比度（灰字浅底/半透明浮层）→ 二值化拉开黑白
            gray = img.convert("L")
            hist = gray.histogram()
            thr = _otsu_threshold(hist, gray.width * gray.height)
            out.append(("二值化", gray.point(lambda v, t=thr: 255 if v > t else 0).convert("RGB")))
    return out


def _run(ps: str, path: str, timeout_s: int) -> str:
    # 名字必须唯一：候选图并行识别时，同毫秒的固定名字会互相覆盖/删掉对方的产出
    out_file = Path(tempfile.gettempdir()) / f"psocr_out_{uuid.uuid4().hex}.txt"
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


def _run_batch(ps: str, paths: list[str], timeout_s: int) -> dict[str, str]:
    """一次 PowerShell 进程识别多张图：省掉每个候选 ~0.4 秒的启动开销。

    路径清单写进 JSON 文件传过去（用 -File 传数组会被 PowerShell 参数绑定拆散）。
    """
    import json

    stamp = int(time.time() * 1000)
    list_file = Path(tempfile.gettempdir()) / f"psocr_list_{stamp}.json"
    out_file = Path(tempfile.gettempdir()) / f"psocr_batch_{stamp}.json"
    try:
        list_file.write_text(json.dumps(paths, ensure_ascii=False), encoding="utf-8")
        proc = subprocess.run(
            [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(PS1), "-ListFile", str(list_file), "-OutFile", str(out_file)],
            capture_output=True, timeout=timeout_s,
        )
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()[-400:]
            raise RuntimeError(f"PowerShell 批量 OCR 失败: {err or proc.returncode}")
        raw = json.loads(out_file.read_text(encoding="utf-8-sig"))
        if isinstance(raw, str):  # 只有一张图时 ConvertTo-Json 不产生对象
            raw = {paths[0]: raw}
        return {str(k): str(v or "").strip() for k, v in raw.items()}
    finally:
        for f in (list_file, out_file):
            try:
                f.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass


def recognize(image_path: str, timeout_s: int = 120, enhance: bool | None = None,
              quality: str = "standard") -> str:
    """识别图片文字。

    quality="high" 时会额外试「放大锐化」「深色反色」「低对比度二值化」，
    多张图只起一个 PowerShell 进程，用 text_score 挑最好的一份；
    quality="standard" 只在结果太短时才补救一次（快，约 0.6 秒）。
    """
    ps = _powershell()
    if not ps:
        raise RuntimeError("找不到 powershell.exe")

    q = "high" if (enhance is True or quality == "high") else "standard"
    best_text, best_score = "", -1e9
    tmp_files: list[str] = []

    try:
        targets: list[tuple[str, str]] = []
        for i, (name, img) in enumerate(build_variants(image_path, q)):
            if name == "原图":
                targets.append((name, image_path))
                continue
            tmp = Path(tempfile.gettempdir()) / f"psocr_var_{int(time.time() * 1000)}_{i}.png"
            img.save(tmp)
            tmp_files.append(str(tmp))
            targets.append((name, str(tmp)))

        # 候选图并行识别：3 个候选串行约 1.4 秒 → 并行约 0.6 秒（每张图各起一个 PowerShell 进程）
        results: dict[str, str] = {}
        if len(targets) == 1:
            try:
                results[targets[0][1]] = _run(ps, targets[0][1], timeout_s)
            except Exception:  # noqa: BLE001
                results = {}
        else:
            with ThreadPoolExecutor(max_workers=min(len(targets), 4)) as ex:
                futures = {ex.submit(_run, ps, p, timeout_s): p for _, p in targets}
                for fut, p in futures.items():
                    try:
                        results[p] = fut.result(timeout=timeout_s)
                    except Exception:  # noqa: BLE001  某个候选失败不影响其它候选
                        continue

        for name, target in targets:
            text = results.get(target)
            if text is None:
                continue
            sc = text_score(text)
            if sc > best_score:
                best_text, best_score = text, sc
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
