"""生成 assets/app.ico / assets/icon*.png（可重复运行）。

设计：深色圆角方块 + 蓝色辉光的字母 Q（Q 本身就形似放大镜），
缩到 16px 仍可辨认，和程序深色主题同色系。
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets"
OUT.mkdir(exist_ok=True)

S = 1024
BG_TOP, BG_BOT = (22, 35, 59), (8, 11, 17)
ACCENT = (77, 157, 255)
TEXT = (233, 242, 255)
FONT = "C:/Windows/Fonts/msyhbd.ttc"

# 竖渐变底
base = Image.new("RGB", (S, S))
px = base.load()
for y in range(S):
    t = y / (S - 1)
    px_row = tuple(int(BG_TOP[i] + (BG_BOT[i] - BG_TOP[i]) * t) for i in range(3))
    for x in range(S):
        px[x, y] = px_row

# 圆角遮罩
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle((0, 0, S - 1, S - 1), radius=224, fill=255)

# 顶部高光，让方块有厚度
gloss = Image.new("L", (S, S), 0)
gd = ImageDraw.Draw(gloss)
gd.ellipse((-S * 0.35, -S * 0.95, S * 1.35, S * 0.55), fill=46)
gloss = gloss.filter(ImageFilter.GaussianBlur(70))
base = Image.composite(Image.new("RGB", (S, S), (255, 255, 255)), base, gloss.point(lambda v: v))

# 字母 Q + 辉光
font = ImageFont.truetype(FONT, 620)
glyph = Image.new("L", (S, S), 0)
gd = ImageDraw.Draw(glyph)
box = gd.textbbox((0, 0), "Q", font=font)
gd.text(((S - (box[2] - box[0])) / 2 - box[0], (S - (box[3] - box[1])) / 2 - box[1] - 10),
        "Q", font=font, fill=255)

glow = glyph.filter(ImageFilter.GaussianBlur(26)).point(lambda v: min(255, int(v * 1.5)))
shadow = Image.new("RGB", (S, S), (6, 10, 18))
shadow.paste(Image.new("RGB", (S, S), ACCENT), (0, 0), glow)
base = Image.composite(shadow, base, glow.point(lambda v: min(255, v)))

layer = Image.new("RGB", (S, S), TEXT)
base = Image.composite(layer, base, glyph)

# 底部一条蓝色细条，暗含"答案已就绪"
bar = Image.new("L", (S, S), 0)
ImageDraw.Draw(bar).rounded_rectangle((S * 0.30, S * 0.855, S * 0.70, S * 0.885),
                                      radius=S * 0.016, fill=230)
base = Image.composite(Image.new("RGB", (S, S), (61, 220, 132)), base, bar)

icon = base.convert("RGBA")
icon.putalpha(mask)

icon.resize((512, 512), Image.LANCZOS).save(OUT / "icon-512.png")
icon.resize((256, 256), Image.LANCZOS).save(OUT / "icon-256.png")
icon.resize((64, 64), Image.LANCZOS).save(OUT / "icon-64.png")
icon.save(OUT / "app.ico", sizes=[(16, 16), (20, 20), (24, 24), (32, 32),
                                  (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)])
print("写入:", *(p.name for p in sorted(OUT.iterdir())))
