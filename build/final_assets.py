from PIL import Image, ImageDraw, ImageFilter
import word_icon as wi

P = r"C:\Users\12518\Doubao\chats\2026-11-23\new-chat-2\RZDCL"

# 主图标：深蓝圆角底板 + RZDCL 字标
big = wi.render(1024, "bahn")
big.save(P + r"\assets\rzdcl.png")
big.save(P + r"\assets\rzdcl_raw.png")

sizes = [256, 128, 64, 48, 32, 24, 16]
frames, strip = [], Image.new("RGBA", (sum(sizes) + 26 * len(sizes) + 26, 320), (240, 242, 245, 255))
x = 26
for s in sizes:
    im = wi.render(s, "bahn", simple=(s <= 24))
    if s <= 64:
        im = im.filter(ImageFilter.UnsharpMask(radius=0.5, percent=45, threshold=2))
    frames.append(im)
    strip.alpha_composite(im, (x, 24))
    x += s + 26
frames[0].save(P + r"\assets\rzdcl.ico", format="ICO", sizes=[(s, s) for s in sizes])
strip.resize((strip.width * 3, strip.height * 3), Image.NEAREST).save(P + r"\图标预览.png")

# 顶栏徽标：青色圆角块 + 白色 R，透明底
S = 512 * 4
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=wi.CYAN)
f = wi.load("bahn", int(S * 0.62))
bw = wi.text_w(d, "R", f, 0)
box = Image.new("RGBA", (S, S), (0, 0, 0, 0))
bd = ImageDraw.Draw(box)
bd.text((S / 2 - bw / 2, S / 2 - int(S * 0.34)), "R", font=f, fill=wi.WHITE)
bb = box.getbbox()
# 用字墨边界把 R 精确居中
dy = int(S / 2 - (bb[1] + bb[3]) / 2)
img.alpha_composite(box, (0, dy))
img.resize((512, 512), Image.LANCZOS).save(P + r"\assets\rzdcl_mark.png")
print("done", [f.size for f in frames])
