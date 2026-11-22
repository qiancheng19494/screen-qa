"""生成主窗顶部横幅（纯 PIL，无外部素材）。v2.19：更克制的设计——左圆点标记 + 字标 + 右侧版本胶囊。"""
import pathlib

from PIL import Image, ImageDraw, ImageFont

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "banner.png"
W, H = 458, 46
FONT_B = r"C:\Windows\Fonts\msyhbd.ttc"
FONT_R = r"C:\Windows\Fonts\msyh.ttc"


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def main() -> int:
    img = Image.new("RGB", (W, H), "#0d1117")
    d = ImageDraw.Draw(img)
    # 左上角极淡的光晕，避免整条死黑
    for y in range(H):
        for x in range(0, W, 2):
            k = max(0.0, 1 - (x / (W * 0.5)) ** 2 - (y / (H * 0.9)) ** 2)
            if k <= 0:
                continue
            base = (0x18, 0x1f, 0x2a)
            c = lerp((0x0d, 0x11, 0x17), base, min(1.0, k * 0.9))
            d.point((x, y), fill=c)
            d.point((x + 1, y), fill=c)
    try:
        fb = ImageFont.truetype(FONT_B, 16)
        fr = ImageFont.truetype(FONT_R, 9)
    except Exception:  # noqa: BLE001
        fb = fr = ImageFont.load_default()

    # 标记：圆点 + 方框，暗示"取景框"
    d.rounded_rectangle([16, 14, 34, 32], radius=5, outline="#4d9dff", width=2)
    d.ellipse([22, 20, 28, 26], fill="#4d9dff")
    d.text((42, 9), "屏幕问答", font=fb, fill="#e6edf3")
    d.text((42, 28), "截屏 · 读题 · 作答", font=fr, fill="#6e7781")

    # 右侧版本胶囊
    ver = "v2.19"
    tw = fr.getlength(ver)
    d.rounded_rectangle([W - 16 - tw - 16, 13, W - 16, 33], radius=10, fill="#17263d")
    d.text((W - 16 - tw - 8, 18), ver, font=fr, fill="#4d9dff")
    # 底部分隔线
    d.line([(0, H - 1), (W, H - 1)], fill="#1f2733")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT)
    print("saved", OUT, img.size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
