"""Фото чата «вовлекли N» — как у Марьи, на её фоне.

Фон (градиент, зерно, завиток, слово «вовлекли») восстановлен с её картинки
от 02.11.2026 без цифр — assets/sensation/plain.png. Сверху: бледная планка
(кремовый, 30 %), крупное число кремовым с мягкой тенью. Шрифт цифр —
Plus Jakarta Sans (ближайший свободный к её шрифту), слово — с оригинала.
Подгонка параметров — по её «86» (см. params.json).
"""

from __future__ import annotations

import io
import json
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ASSETS = Path(__file__).parent / "assets" / "sensation"
SS = 4                     # рисуем крупнее и уменьшаем — ровные края


@lru_cache(maxsize=1)
def _params() -> dict:
    return json.loads((ASSETS / "params.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _plain() -> np.ndarray:
    return np.asarray(Image.open(ASSETS / "plain.png").convert("RGB")).astype(np.float32)


def _alpha(text: str, spec: dict, size: tuple[int, int]) -> np.ndarray:
    """Маска текста 0..1: высота «8» = cap, растяжение sx, трекинг, центр cx, базовая линия base."""
    W, H = size
    font = str(ASSETS / _params()["font"])
    f0 = ImageFont.truetype(font, 1000)
    f0.set_variation_by_axes([spec["wght"]])
    bb = f0.getbbox("8", anchor="ls")
    fsize = round(1000 * spec["cap"] * SS / (bb[3] - bb[1]))
    f = ImageFont.truetype(font, fsize)
    f.set_variation_by_axes([spec["wght"]])
    sx = spec["sx"]
    widths = [f.getlength(ch) * sx for ch in text]
    total = sum(widths) + spec["track"] * SS * (len(text) - 1)
    room = 0.9 * W * SS
    if total > room:                     # трёхзначное не влезает — мельче, по той же базовой линии
        fsize = int(fsize * room / total)
        f = ImageFont.truetype(font, fsize)
        f.set_variation_by_axes([spec["wght"]])
        widths = [f.getlength(ch) * sx for ch in text]
        total = sum(widths) + spec["track"] * SS * (len(text) - 1)
    canvas = Image.new("L", (W * SS, H * SS), 0)
    x = spec["cx"] * SS - total / 2
    for ch, w in zip(text, widths):
        g = Image.new("L", (int(w / sx) + 4 * SS, H * SS), 0)
        ImageDraw.Draw(g).text((0, spec["base"] * SS), ch, font=f, fill=255, anchor="ls")
        g = g.resize((max(1, round(g.width * sx)), g.height), Image.LANCZOS)
        canvas.paste(g, (round(x), 0), g)
        x += w + spec["track"] * SS
    return np.asarray(canvas.resize((W, H), Image.LANCZOS)).astype(np.float32) / 255


def render(count: int, goal: int | None) -> bytes:
    """JPEG 640×640: «вовлекли {count}» поверх бледной планки {goal}."""
    P = _params()
    bg = _plain().copy()
    H, W, _ = bg.shape
    if goal:
        a = _alpha(str(goal), P["goal"], (W, H))[..., None] * P["goal_alpha"]
        bg = bg * (1 - a) + np.array(P["goal_color"], np.float32) * a
    a = _alpha(str(count), P["number"], (W, H))
    # мягкая тень под числом, как у оригинала
    blur = Image.fromarray((a * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(P["shadow"]["sigma"]))
    shade = 1 - P["shadow"]["k"] * (np.asarray(blur).astype(np.float32) / 255)
    bg = bg * shade[..., None]
    a3 = a[..., None]
    out = bg * (1 - a3) + np.array(P["cream"], np.float32) * a3
    buf = io.BytesIO()
    Image.fromarray(np.clip(np.round(out), 0, 255).astype(np.uint8)).save(buf, "JPEG", quality=93)
    return buf.getvalue()
