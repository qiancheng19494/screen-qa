"""把程序打包成免安装的 exe（onedir 版：文件夹里有 屏幕问答.exe + _internal）。

用法：
    python tools/build_exe.py          # 生成 dist/屏幕问答/屏幕问答.exe
    python tools/build_exe.py --onefile  # 单文件 exe（部分受限环境解压会失败，慎用）

为什么默认 onedir：启动快（1–2 秒）、不需要往 %TEMP% 解压、
杀软误报也少。想拷给别人时整个"屏幕问答"文件夹一起拷走即可。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME_ASCII = "sqa"          # PyInstaller 内部名（避免中文名在命令行被转码弄坏）
NAME_FINAL = "屏幕问答"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--onefile", action="store_true", help="打包成单个 exe")
    args = ap.parse_args()

    build = ROOT / "_pyi"
    dist = ROOT / "dist"
    datas = [
        (ROOT / "psocr.ps1", "."),
        (ROOT / "assets", "assets"),
        (ROOT / "tools" / "cli_shims", "tools/cli_shims"),
        (ROOT / "README.md", "."),
        (ROOT / "docs", "docs"),
    ]

    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--onefile" if args.onefile else "--onedir",
        "--windowed", "--name", NAME_ASCII,
        "--icon", str(ROOT / "assets" / "app.ico"),
        "--distpath", str(dist), "--workpath", str(build), "--specpath", str(build),
    ]
    for src, dest in datas:
        if src.exists():
            cmd += ["--add-data", f"{src};{dest}"]
    cmd.append(str(ROOT / "app.py"))

    print("→", " ".join(cmd[:8]), "...")
    rc = subprocess.call(cmd, cwd=str(ROOT))
    if rc != 0:
        print(f"PyInstaller 失败（退出码 {rc}）")
        return rc

    # 改成本名的输出
    final = dist / NAME_FINAL
    if final.exists():
        shutil.rmtree(final)
    built = dist / NAME_ASCII
    if args.onefile:
        exe = dist / f"{NAME_ASCII}.exe"
        if exe.is_file():
            final.mkdir(parents=True, exist_ok=True)
            exe.rename(final / f"{NAME_FINAL}.exe")
            shutil.copy2(ROOT / "config.json", final / "config.json")
    else:
        built.rename(final)
        exe = final / f"{NAME_ASCII}.exe"
        if exe.is_file():
            exe.rename(final / f"{NAME_FINAL}.exe")
        cfg = ROOT / "config.json"
        if cfg.is_file():
            shutil.copy2(cfg, final / "config.json")

    total = sum(p.stat().st_size for p in final.rglob("*") if p.is_file())
    print(f"\n✅ 完成：{final}\\{NAME_FINAL}.exe")
    print(f"   整个文件夹 {total/1e6:.1f} MB —— 要拷给别人就整个文件夹一起拷")
    return 0


if __name__ == "__main__":
    sys.exit(main())
