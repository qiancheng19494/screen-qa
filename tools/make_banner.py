"""生成主窗顶部横幅（纯 PIL，无外部素材）。"""
import pathlib
from PIL import Image, ImageDraw, ImageFont

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "banner.png"
W, H = 490, 52
FONT = r"C:\Windows\Fonts\msyhbd.ttc"
FONT2 = r"C:\Windows\Fonts\msyh.ttc"


def main() -> int:
    img = Image.new("RGB", (W, H), "#0b0f15")
    d = ImageDraw.Draw(img)
    # 左侧渐隐的强调色斜带
    for x in range(W):
        k = max(0.0, 1 - x / (W * 0.62))
        c = (int(0x0b + (0x25 - 0x0b) * k), int(0x0f + (0x3a - 0x0f) * k),
             int(0x15 + (0x5c - 0x15) * k))
        d.line([(x, 0), (x, H)], fill=c)
    # 细网格纹理
    for x in range(0, W, 18):
        d.line([(x, 0), (x, H)], fill=(255, 255, 255, 6) if False else "#101722")
    try:
        f1 = ImageFont.truetype(FONT, 17)
        f2 = ImageFont.truetype(FONT2, 10)
    except Exception:
        f1 = f2 = ImageFont.load_default()
    d.text((16, 8), "屏幕问答", font=f1, fill="#eaf1ff")
    d.text((16, 32), "截屏 · 读题 · 作答", font=f2, fill="#7f8ea3")
    d.text((W - 14, 12), "DeepSeek", font=f2, fill="#4d9dff", anchor="ra")
    d.text((W - 14, 28), "v2.15", font=f2, fill="#3ddc84", anchor="ra")
    d.line([(0, H - 1), (W, H - 1)], fill="#1d2836")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT)
    print("saved", OUT, img.size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
