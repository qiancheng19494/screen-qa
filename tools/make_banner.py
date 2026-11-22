"""生成主窗顶部横幅：assets/banner.png（深色）与 assets/banner-light.png（浅色）。"""
import pathlib
from PIL import Image, ImageDraw, ImageFont

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "assets"
W, H = 458, 46
FB = r"C:\Windows\Fonts\msyhbd.ttc"
FR = r"C:\Windows\Fonts\msyh.ttc"


def render(dark: bool) -> Image.Image:
    bg = "#0d1117" if dark else "#ffffff"
    fg = "#eaf1ff" if dark else "#111827"
    sub = "#7f8ea3" if dark else "#8b95a7"
    acc = "#4d9dff" if dark else "#3b82f6"
    acc_bg = "#16233a" if dark else "#e6efff"
    hair = "#1f2733" if dark else "#e3e9f2"
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    try:
        f1 = ImageFont.truetype(FB, 15)
        f2 = ImageFont.truetype(FR, 9)
        f3 = ImageFont.truetype(FR, 8)
    except Exception:
        f1 = f2 = f3 = ImageFont.load_default()
    # 左侧取景框标记
    d.rounded_rectangle([12, 12, 34, 34], radius=6, outline=acc, width=2)
    d.rounded_rectangle([17, 17, 29, 29], radius=2, fill=acc)
    d.text((42, 9), "屏幕问答", font=f1, fill=fg)
    d.text((42, 28), "截屏 · 读题 · 作答", font=f3, fill=sub)
    # 右侧版本胶囊
    label = "v2.20"
    tw = d.textlength(label, font=f2)
    d.rounded_rectangle([W - 26 - tw - 12, 15, W - 14, 33], radius=9, fill=acc_bg)
    d.text((W - 26 - tw - 6, 18), label, font=f2, fill=acc)
    d.line([(0, H - 1), (W, H - 1)], fill=hair)
    return img


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    render(True).save(OUT / "banner.png")
    render(False).save(OUT / "banner-light.png")
    print("saved banner.png / banner-light.png", (W, H))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
