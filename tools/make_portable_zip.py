"""把 dist/屏幕问答（onedir 打包产物）压成一个绿色版 zip，可直接发人。"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "屏幕问答"


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    if not DIST.is_dir():
        print("找不到 dist/屏幕问答：先跑 python tools/build_exe.py")
        return 1
    out = ROOT / "dist" / "屏幕问答-绿色版.zip"
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in DIST.rglob("*"):
            if path.is_dir():
                continue
            arc = Path("屏幕问答") / path.relative_to(DIST)
            zf.write(path, str(arc))
            n += 1
        readme = ROOT / "启动.bat"
        if readme.exists():
            pass
    size = out.stat().st_size
    print(f"已生成 {out.name}：{n} 个文件，{size / 1048576:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
