"""«Зал ММ8» — 110 кресел, в каждом фото участника ступени, который оплатил.

Просьба Глеба (голосовое, переслала Лилия 15.10.2026): визуализация зала,
ссылку кидают участникам вместе с новостями — пусть видят, что зал заполняется
их ребятами и каких мест не хватает. Наводишь на кресло — коротко (имя, город,
ниша, доход), нажимаешь — подробнее.

Источник — Google-таблица Лилии со всеми участниками ступеней (открыта по ссылке,
читается экспортом xlsx). Фото — картинки, вставленные в колонку «Фото».
Страница без входа, по секретной ссылке на своём адресе (HOST); таблица перечитывается в фоне раз в 10 минут.

Показываем только то, что не стыдно увидеть соседу по залу. Телефон, email,
дату рождения, платёж, боли, «Точку Б», ожидания, комментарии и «риск» — никогда.
«Истинные мотивы» показываем как «Куда идёт» (решение Станислава 25.10).
"""

from __future__ import annotations

import io
import json
import re
import secrets
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from . import store

SHEET = "1M53_JiC0h7A6AFXQ_Bhfnn3e7qPhGcy-tb7BpM7nRUQ"
# Зал живёт на своём адресе, чтобы по ссылке не было видно платформу.
# Caddy (/opt/sites/zal.caddy) пускает туда только /<ключ> и /hall/*; на домене платформы /hall — 404.
HOST = "hall.example.com"
DIR = Path("/data/hall")
SEATS = 110                                  # было 81 → 101 (Лилия 19.10) → 106 (Марья 28.10) → 110 (+4 «не в планке», Марья 29.10)
ROWS = (7, 10, 13, 16, 19, 21, 24)          # полукругом, от сцены к последнему ряду; в ряду — по длине дуги
# разделы под основным списком — с них зал не читает, даже если у строки есть номер
STOP = ("только на разбор", "отказ")
# кроме «менторы» и «не в планке»: вовлечения менторов (Марья 28.10) и те, кто «не в планке» (Марья 29.10), тоже в зале
MENTORS = "ментор"
OFFPLAN = "не в планке"
FRESH = 5 * 60                              # запасной случай: фон встал — перечитать при открытии
_LOCK = threading.Lock()

# колонки таблицы — по заголовку, а не по номеру: Лилия может вставить столбец
COLS = {
    "no": "№", "name": "имя фамилия", "account": "аккаунт", "photo": "фото", "paid": "оплачено", "city": "город",
    "inviter": "кто вовлек", "profit": "чистая прибыль", "revenue": "доход", "niche": "ниша",
    "team": "команда", "goal": "исти",   # «Куда идёт» — «Истинные мотивы» (Станислав 25.10); в таблице «Истиные» — ловим оба
}


_KEY: str | None = None


def key(conn=None) -> str:
    """Секрет в адресе: без него страницу не найти. Запоминаем в памяти —
    фото грузятся десятками одновременно, и база на каждом запросе падала (500)."""
    global _KEY
    if _KEY:
        return _KEY
    own = conn is None
    conn = conn or store.connect()
    try:
        k = store.get_setting(conn, "hall:key", "")
        if not k:
            k = secrets.token_urlsafe(10)
            store.set_setting(conn, "hall:key", k)
    finally:
        if own:
            conn.close()
    _KEY = k
    return k


def _clean(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        v = int(v) if v == int(v) else v
    if isinstance(v, int):
        return f"{v:,}".replace(",", " ")
    s = re.sub(r"[ \t]+", " ", str(v)).strip()
    return "" if s in ("-", "_", "—", "None") else s


def _money(v) -> str:
    """«500.000», 2500000.0, «300000-400000» → читаемо; слова («Не знает», «Пенсия») — как есть."""
    s = _clean(v)
    if not s:
        return ""
    if re.fullmatch(r"[\d .,\-–+]+", s) and any(ch.isdigit() for ch in s):
        parts = re.split(r"\s*[-–]\s*", s.rstrip("+"))
        nums = []
        for p in parts:
            digits = re.sub(r"\D", "", p)
            if digits:
                nums.append(f"{int(digits):,}".replace(",", " "))
        out = " – ".join(nums)
        return out + ("+" if s.endswith("+") else "") + " ₽"
    return s


def _first_line(s: str, limit: int = 70) -> str:
    s = s.split("\n")[0].strip()
    return s if len(s) <= limit else s[:limit].rsplit(" ", 1)[0] + "…"


def _photo(img, path: Path) -> bool:
    """Картинка из таблицы → квадрат 256 px (лица обычно в верхней части кадра)."""
    try:
        from PIL import Image, ImageOps
        im = Image.open(io.BytesIO(img._data()))
        im = ImageOps.exif_transpose(im).convert("RGB")
        w, h = im.size
        side = min(w, h)
        top = 0 if h > w else (h - side) // 2
        im = im.crop(((w - side) // 2, top, (w - side) // 2 + side, top + side)).resize((256, 256))
        im.save(path, "JPEG", quality=85)
        return True
    except Exception:
        return False


def sync() -> dict[str, Any]:
    """Перечитать таблицу Лилии: кто оплатил, фото, короткие сведения."""
    import openpyxl
    from .proclock import held
    with _LOCK, held("hall"):
        req = urllib.request.Request(f"https://docs.google.com/spreadsheets/d/{SHEET}/export?format=xlsx",
                                     headers={"User-Agent": "vershina-hall"})
        data = urllib.request.urlopen(req, timeout=60).read()
        wb = openpyxl.load_workbook(io.BytesIO(data))
        ws = wb.worksheets[0]
        head = [(_clean(c) or "").lower() for c in next(ws.iter_rows(min_row=1, max_row=1, values_only=True))]
        col = {k: next((i for i, h in enumerate(head) if h.startswith(v)), None) for k, v in COLS.items()}
        if col["name"] is None or col["paid"] is None:
            raise RuntimeError("В таблице не нашлось колонок «Имя фамилия» и «Оплачено».")
        if col["no"] is None and col["name"] != 0:
            col["no"] = 0                         # номера — первая колонка, даже если заголовок «№» стёрли (19.10)
        pics = {im.anchor._from.row + 1: im for im in ws._images if im.anchor._from.col == (col["photo"] or 0)}
        tmp = DIR / "photos.new"
        tmp.mkdir(parents=True, exist_ok=True)
        for f in tmp.glob("*"):
            f.unlink()
        people, mentors = [], False
        for n, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
            get = lambda k: row[col[k]] if col[k] is not None and col[k] < len(row) else None
            name = _clean(get("name"))
            if not name:
                continue
            # зал — только основной список. Ниже него разделы-подзаголовки без номера
            # («менторы», «не в планке», «отказ» — 19.10): с первого такого — стоп
            if name.lower().startswith(MENTORS) and not _clean(get("no")):
                mentors = True                   # подзаголовок «менторы» — дальше их вовлечения
                continue
            if name.lower().startswith(OFFPLAN) and not _clean(get("no")):
                mentors = False                  # «не в планке» — оплатившие, садятся как основной список
                continue
            if name.lower().startswith(STOP) or not _clean(get("no")):
                break
            # «возврат» в основном списке — пока только заявлен, место за ним (Лилия 19.10);
            # подтверждённые возвраты переносят вниз, в «отказ», — туда чтение не доходит
            # у вовлечений менторов «Оплачено» бывает пустым — Марья считает весь раздел
            status = _clean(get("paid")).lower()
            if status != "оплачено" and not status.startswith("возврат") and not (mentors and not status):
                continue
            photo = None
            if n in pics and _photo(pics[n], tmp / f"{len(people) + 1}.jpg"):
                photo = f"{len(people) + 1}.jpg"
            people.append({
                "name": name, "city": _clean(get("city")), "niche": _clean(get("niche")),
                "niche_short": _first_line(_clean(get("niche"))), "revenue": _money(get("revenue")),
                "profit": _money(get("profit")), "team": _clean(get("team")), "goal": _clean(get("goal")),
                "inviter": _clean(get("inviter")), "photo": photo,
            })
        old = DIR / "photos"
        if old.exists():
            for f in old.glob("*"):
                f.unlink()
            old.rmdir()
        tmp.rename(old)
        out = {"at": datetime.now().isoformat(timespec="seconds"), "people": people[:SEATS],
               "extra": max(0, len(people) - SEATS)}
        (DIR / "data.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        return out


def data() -> dict[str, Any]:
    """Сведения для страницы; устаревшие — перечитать в фоне, показать последние."""
    path = DIR / "data.json"
    if not path.exists():
        try:
            return sync()
        except Exception as e:                       # таблица недоступна — пустой зал с пояснением
            return {"at": None, "people": [], "error": str(e)}
    d = json.loads(path.read_text(encoding="utf-8"))
    if time.time() - path.stat().st_mtime > FRESH and not _LOCK.locked():
        threading.Thread(target=_quiet_sync, daemon=True).start()
    return d


def start_background() -> None:
    """Перечитывать таблицу Лилии раз в 2 минуты, пока работает приложение, —
    тот, кто открыл ссылку, сразу видит свежий зал. Чтение — секунды
    (раньше было раз в 10 минут, и Лилия замечала задержку, 19.10)."""
    def loop():
        while True:
            _quiet_sync()
            time.sleep(2 * 60)
    threading.Thread(target=loop, daemon=True, name="hall-sync").start()


def _quiet_sync() -> None:
    try:
        sync()
    except Exception:
        pass


def seats(people: list[dict]) -> list[dict]:
    """Кресла полукругом: координаты для SVG (сцена внизу по центру).
    Садим от сцены и от центра ряда — первые оплатившие в первом ряду посередине."""
    import math
    out = []
    cx, cy = 500, 610
    for r, n in enumerate(ROWS):
        radius = 190 + r * 62
        span = math.radians(150)
        for k in range(n):
            a = math.radians(90) + span / 2 - span * (k + 0.5) / n
            out.append({"x": round(cx + radius * math.cos(a), 1), "y": round(cy - radius * math.sin(a), 1),
                        "row": r + 1, "no": k + 1, "p": None, "_c": abs(k - (n - 1) / 2)})
    for i, seat in enumerate(sorted(out, key=lambda s: (s["row"], s["_c"], s["no"]))):
        seat["order"] = i                           # какое по счёту место займёт следующий оплативший
        if i < len(people):
            seat["p"] = dict(people[i], i=i)
    return out
