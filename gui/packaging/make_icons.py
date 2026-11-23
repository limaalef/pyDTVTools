"""Gera packaging/icon.png / icon.ico / icon.icns (desenho próprio: tela de TV com triângulo de 'play'). Uso: python packaging/make_icons.py"""
from pathlib import Path
from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent


def draw(size: int = 1024) -> Image.Image:
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    # fundo: quadrado arredondado com degradê vertical
    bg = Image.new("RGBA", (s, s))
    px = bg.load()
    top, bot = (24, 52, 94), (10, 22, 44)
    for y in range(s):
        t = y / (s - 1)
        c = tuple(round(top[i] * (1 - t) + bot[i] * t) for i in range(3)) + (255,)
        for x in range(s):
            px[x, y] = c
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle((s * 0.04, s * 0.04, s * 0.96, s * 0.96), radius=s * 0.22, fill=255)
    img.paste(bg, (0, 0), mask)
    d = ImageDraw.Draw(img)
    # tela
    d.rounded_rectangle((s * 0.16, s * 0.24, s * 0.84, s * 0.66), radius=s * 0.06, outline=(76, 194, 255, 255), width=round(s * 0.035))
    # play
    d.polygon([(s * 0.43, s * 0.34), (s * 0.43, s * 0.56), (s * 0.62, s * 0.45)], fill=(76, 194, 255, 255))
    # pé do suporte
    d.rounded_rectangle((s * 0.36, s * 0.72, s * 0.64, s * 0.76), radius=s * 0.02, fill=(76, 194, 255, 255))
    # barra de progresso
    d.rounded_rectangle((s * 0.22, s * 0.82, s * 0.78, s * 0.86), radius=s * 0.02, fill=(255, 255, 255, 50))
    d.rounded_rectangle((s * 0.22, s * 0.82, s * 0.56, s * 0.86), radius=s * 0.02, fill=(95, 211, 141, 255))
    return img


if __name__ == "__main__":
    big = draw(1024)
    big.resize((256, 256), Image.LANCZOS).save(OUT / "icon.png")
    big.save(OUT / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    big.save(OUT / "icon.icns")
    print("ícones gerados em", OUT)
