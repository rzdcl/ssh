from PIL import Image, ImageDraw, ImageFont, ImageFilter

NAVY = (11, 28, 51, 255)
CYAN = (43, 212, 230, 255)
WHITE = (255, 255, 255, 255)
WORD = "RZDCL"

FONTS = {
    "bahn": (r"C:\Windows\Fonts\bahnschrift.ttf", ["Bold", "SemiBold"]),
    "segoe": (r"C:\Windows\Fonts\segoeuib.ttf", []),
    "ariblk": (r"C:\Windows\Fonts\ariblk.ttf", []),
}


def load(kind, px):
    path, vars_ = FONTS[kind]
    f = ImageFont.truetype(path, px)
    if vars_:
        for v in vars_:
            try:
                f.set_variation_by_name(v)
                break
            except Exception:
                continue
    return f


def text_w(d, s, f, track):
    return sum(d.textlength(c, font=f) for c in s) + track * (len(s) - 1)


def draw_word(d, cx, cy, s, f, track, fill):
    w = text_w(d, s, f, track)
    x = cx - w / 2
    asc, desc = f.getmetrics()
    y = cy - (asc + desc) / 2
    for c in s:
        d.text((x, y), c, font=f, fill=fill)
        x += d.textlength(c, font=f) + track
    return w


def render(size, kind="bahn", ss=4, simple=False):
    S = size * ss
    u = S / 1024.0
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(226 * u), fill=NAVY)

    target = (898 if not simple else 910) * u
    lo, hi = 10, int(400 * u)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        f = load(kind, mid)
        if text_w(d, WORD, f, -0.012 * mid) <= target:
            lo = mid
        else:
            hi = mid
    f = load(kind, lo)
    track = -0.012 * lo

    # 先在透明层上排字，拿到真实字墨边界，再整体居中
    tmp = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    td = ImageDraw.Draw(tmp)
    asc, desc = f.getmetrics()
    w = draw_word(td, S / 2, asc + 40 * u, WORD, f, track, WHITE)
    box = tmp.getbbox()
    cap_h = box[3] - box[1]

    bar_w = w * 0.5
    bar_h = 30 * u
    gap = 46 * u
    total = cap_h + gap + bar_h
    top = S / 2 - total / 2
    dy = int(top - box[1])
    img.alpha_composite(tmp, (0, dy))

    y1 = top + cap_h + gap
    d.rounded_rectangle([S / 2 - bar_w / 2, y1, S / 2 + bar_w / 2, y1 + bar_h],
                        radius=bar_h / 2, fill=CYAN)
    return img.resize((size, size), Image.LANCZOS)


def sheet():
    sizes = [16, 24, 32, 48, 64, 128, 256]
    rows = []
    for kind in ("bahn", "segoe", "ariblk"):
        row = Image.new("RGBA", (sum(sizes) + 24 * len(sizes) + 24, 300), (240, 242, 245, 255))
        x = 24
        for s in sizes:
            im = render(s, kind, simple=(s <= 24))
            if s <= 64:
                im = im.filter(ImageFilter.UnsharpMask(radius=0.5, percent=40, threshold=2))
            row.alpha_composite(im, (x, 22))
            x += s + 24
        rows.append(row)
    out = Image.new("RGBA", (max(r.width for r in rows), sum(r.height for r in rows)), (255, 255, 255, 255))
    y = 0
    for r in rows:
        out.alpha_composite(r, (0, y))
        y += r.height
    out.save(r"C:\Users\12518\Doubao\chats\2026-11-23\new-chat-2\RZDCL\build\_word_cmp.png")


if __name__ == "__main__":
    for kind in ("bahn", "segoe", "ariblk"):
        render(1024, kind).save(rf"C:\Users\12518\Doubao\chats\2026-11-23\new-chat-2\RZDCL\build\_w_{kind}.png")
    sheet()
    print("ok")
