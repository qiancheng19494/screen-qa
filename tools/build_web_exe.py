"""Package the WebView UI into an onedir exe.  Usage: python tools/build_web_exe.py"""
import os, shutil, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "SQA-Web"

def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    work = Path(tempfile.mkdtemp(prefix="webspec-"))
    data = ["%s%suI" % (ROOT / "ui", os.pathsep)]
    data = ["%s%sui" % (ROOT / "ui", os.pathsep)]
    for extra in ("assets", "tools/cli_shims", "docs"):
        p = ROOT / extra
        if p.exists():
            data.append("%s%s%s" % (p, os.pathsep, extra))
    for f in ("psocr.ps1", "README.md"):
        p = ROOT / f
        if p.exists():
            data.append("%s%s." % (p, os.pathsep))
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--onedir", "--windowed", "--name", NAME,
           "--icon", str(ROOT / "assets" / "app.ico"),
           "--hidden-import", "webview",
           "--collect-all", "rapidocr_onnxruntime",
           "--collect-all", "onnxruntime",
           "--hidden-import", "onnxruntime.capi._pybind_state",
           "--distpath", str(ROOT / "dist-web"),
           "--workpath", str(work), "--specpath", str(work)]
    for d in data:
        cmd += ["--add-data", d]
    cmd.append(str(ROOT / "app_web.py"))
    print(" ".join(cmd[:10]), "...")
    r = subprocess.run(cmd, cwd=str(ROOT))
    src = ROOT / "dist-web" / NAME
    exe = src / (NAME + ".exe")
    if exe.exists():
        cfg = ROOT / "config.json"
        if cfg.exists():
            shutil.copy2(cfg, src / "config.json")
        size = sum(f.stat().st_size for f in src.rglob("*") if f.is_file())
        print("OK %s  folder %.1f MB" % (exe, size / 1024 / 1024))
    return r.returncode

if __name__ == "__main__":
    raise SystemExit(main())
