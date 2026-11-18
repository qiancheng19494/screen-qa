"""生成一张中文选择题测试图，用来验证 OCR + 作答链路。"""
from PIL import Image, ImageDraw, ImageFont

W, H = 900, 420
img = Image.new("RGB", (W, H), "white")
d = ImageDraw.Draw(img)
font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 26)
small = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 22)

lines = [
    "12. 某商品的成本为 80 元，按标价的八折出售，仍可获利 20%，",
    "    则该商品的标价是多少元？",
    "",
    "    A. 100 元      B. 120 元      C. 150 元      D. 160 元",
]
y = 40
for ln in lines:
    d.text((40, y), ln, fill="black", font=font if ln.startswith("12.") else small)
    y += 56

img.save("_zh_test.png")
print("saved _zh_test.png", img.size)
