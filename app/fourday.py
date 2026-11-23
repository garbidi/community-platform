"""4-дневный мастермайнд — первый этап потока (для ММ8: 8–11 ноября, ~80 человек).

Так его и называем — не «ступень» (Лилия и Марья, 16.10.2026). Команд и менторов
на нём нет: поток — плоский список участников (flows.kind = '4 дня').

- Участники приходят по одноразовым ссылкам (app/join.py): организатор создаёт
  ссылку на человека и шлёт сам; человек регистрируется через Telegram, организатор
  «пускает» — появляется карточка. К Google-таблицам поток не привязан (Станислав,
  16.10.2026). Импорт из таблицы остался в коде на случай, если понадобится
  (setting fourday:sheet:<flow>), но по умолчанию выключен.
- Фото: из таблицы, загруженное при регистрации или аватарка из телеграма.
  Показывается в карточке и подставляется в «Сенсационные новости».
- Разбор по записи (app/reviews.py) → выжимка и черновик целей прямо в декларацию.
- После 4 дней: «Идёт в ММ8: да / нет / думает» и «Перевести» — человек вместе
  с декларацией переходит в основной поток, в выбранную команду. Кто не пошёл
  дальше, остаётся здесь историей.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import sqlite3
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from . import decl, flows, store
from .config import APP_DB

KIND = "4 дня"
# фото участника: <people.id>.jpg — рядом с базой: проверка на копии базы не должна
# класть фото с чужими номерами в боевую папку (19.10 так и случилось)
PHOTOS = APP_DB.parent / "people_photos"
SHEET_DEFAULT = ""          # по умолчанию без таблицы: участники — по одноразовым ссылкам (решение Станислава 16.10)
NEXT = ("да", "нет", "думает")

# анкета из таблицы — по заголовкам колонок (Лилия может вставлять столбцы)
COLS = {
    "name": "имя фамилия", "phone": "номер телефона", "account": "аккаунт", "photo": "фото",
    "paid": "оплачено", "birthday": "дата рождения", "city": "город", "email": "email",
    "inviter": "кто вовлек", "profit": "чистая прибыль", "revenue": "доход", "niche": "ниша",
    "team": "команда", "point_a": "точка а", "point_b": "точка б", "motives": "истиные мотивы",
    "expect": "ожидание от мм", "format": "комфортный формат", "knows_mm": "знаете ли про основной",
    "mentor": "ментор", "risk": "low / high", "mentor_note": "комментарий ментора", "comment": "комментарий",
    "zoom": "запись зум", "no": "№",
}
# «Комментарий» и «Комментарий ментора» начинаются одинаково — сначала ищем точное совпадение заголовка
EXACT = ("comment", "mentor")
# что видят организаторы в карточке («Из анкеты»); телефон, email и день рождения — отдельно
PROFILE = [("city", "Город"), ("niche", "Ниша / бизнес"), ("revenue", "Доход (выручка)"), ("profit", "Чистая прибыль"),
           ("team", "Команда"), ("inviter", "Кто вовлёк"), ("point_a", "Точка А и боли"), ("point_b", "Точка Б"),
           ("motives", "Истинные мотивы"), ("expect", "Ожидание от ММ"), ("format", "Формат с ментором"),
           ("knows_mm", "Знает про основной ММ"), ("mentor", "Ментор 4 дней"), ("risk", "Риск"),
           ("mentor_note", "Комментарий ментора"), ("comment", "Комментарий"), ("zoom", "Запись зума")]


def init(conn: sqlite3.Connection) -> None:
    if "kind" not in {r[1] for r in conn.execute("PRAGMA table_info(flows)")}:
        conn.execute("ALTER TABLE flows ADD COLUMN kind TEXT NOT NULL DEFAULT 'поток'")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(people)")}
    for col, typ in (("phone", "TEXT"), ("photo", "TEXT"), ("profile", "TEXT"),
                     ("next_flow", "TEXT"), ("moved_to", "INTEGER"), ("moved_from", "INTEGER"),
                     ("next_at", "TEXT"), ("next_by", "TEXT")):
        if col not in cols:
            conn.execute(f"ALTER TABLE people ADD COLUMN {col} {typ}")


def is_fourday(f) -> bool:
    return bool(f) and (f["kind"] if "kind" in f.keys() else "поток") == KIND


def phone_key(v) -> str | None:
    """Телефон для сравнения: последние 10 цифр (8 916… и +7 916… — один номер)."""
    d = re.sub(r"\D", "", str(v or ""))
    if isinstance(v, float):
        d = re.sub(r"\D", "", f"{v:.0f}")
    return d[-10:] if len(d) >= 10 else None


def nick(v) -> str | None:
    s = str(v or "").strip().lstrip("@").strip()
    s = re.sub(r"^https?://t\.me/", "", s)
    return s.lower() if re.fullmatch(r"[A-Za-z0-9_]{4,32}", s) else None


def _txt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        v = int(v) if v == int(v) else v
    s = re.sub(r"[ \t]+", " ", str(v)).strip()
    return "" if s in ("-", "_", "—", "None") else s


# ----------------------------------------------------------------- фото

def photo_path(pid: int) -> Path:
    return PHOTOS / f"{pid}.jpg"


def save_photo(conn, pid: int, data: bytes) -> bool:
    """Любая картинка → квадрат 512 px JPEG (лицо обычно сверху — режем верх)."""
    try:
        from PIL import Image, ImageOps
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
        w, h = im.size
        side = min(w, h)
        top = 0 if h > w else (h - side) // 2
        im = im.crop(((w - side) // 2, top, (w - side) // 2 + side, top + side)).resize((512, 512))
        PHOTOS.mkdir(parents=True, exist_ok=True)
        im.save(photo_path(pid), "JPEG", quality=86)
    except Exception:
        return False
    conn.execute("UPDATE people SET photo=? WHERE id=?", (f"{pid}.jpg", pid))
    return True


def photo_from_url(conn, pid: int, url: str) -> bool:
    """Аватарка из телеграма (photo_url от «Войти через Telegram»), если своего фото нет."""
    if not url or photo_path(pid).exists():
        return False
    try:
        data = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "vershina"}), timeout=15).read()
    except Exception:
        return False
    return save_photo(conn, pid, data)


# ----------------------------------------------------------------- таблица участников

def sheet_of(conn, flow_id: int) -> str:
    return store.get_setting(conn, f"fourday:sheet:{flow_id}", SHEET_DEFAULT) or ""


def set_sheet(conn, flow_id: int, link: str) -> None:
    sid = flows.sheet_id_of(link) if (link or "").strip() else ""
    if sid is None:
        raise ValueError("Не похоже на ссылку на Google-таблицу.")
    store.set_setting(conn, f"fourday:sheet:{flow_id}", sid)


def sync(conn, flow_id: int) -> str:
    """Перечитать таблицу участников: завести новых, обновить анкету и фото,
    «возврат» — выбыл. Карточки на платформе не удаляем."""
    import openpyxl
    f = flows.get(conn, flow_id)
    sid = sheet_of(conn, flow_id)
    if not f or not sid:
        return "таблица участников не задана"
    data = urllib.request.urlopen(urllib.request.Request(
        f"https://docs.google.com/spreadsheets/d/{sid}/export?format=xlsx", headers={"User-Agent": "vershina"}),
        timeout=60).read()
    ws = openpyxl.load_workbook(io.BytesIO(data)).worksheets[0]
    head = [_txt(c).lower() for c in next(ws.iter_rows(min_row=1, max_row=1, values_only=True))]
    col = {k: (next((i for i, h in enumerate(head) if h == v), None) if k in EXACT else None)
           or next((i for i, h in enumerate(head) if h.startswith(v)), None) for k, v in COLS.items()}
    if col["name"] is None or col["paid"] is None:
        raise RuntimeError("В таблице нет колонок «Имя фамилия» и «Оплачено».")
    if col["no"] is None and col["name"] != 0:
        col["no"] = 0                     # номера — первая колонка, даже если заголовок «№» стёрли
    pics = {im.anchor._from.row + 1: im for im in ws._images if col["photo"] is not None and im.anchor._from.col == col["photo"]}
    # кто в списке — ровно как в зале (hall.py): основной список, раздел «менторы»
    # (их вовлечения, «Оплачено» бывает пустым) и «не в планке»; с «только на разбор» / «отказ» — стоп
    from .hall import MENTORS, OFFPLAN, STOP
    added = updated = left = 0
    mentors_part = False
    for n, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        get = lambda k: row[col[k]] if col[k] is not None and col[k] < len(row) else None
        name = _txt(get("name"))
        if not name:
            continue
        low, numbered = name.lower(), bool(_txt(get("no")))
        if low.startswith(MENTORS) and not numbered:
            mentors_part = True
            continue
        if low.startswith(OFFPLAN) and not numbered:
            mentors_part = False
            continue
        if low.startswith(STOP) or not numbered:
            break
        paid = _txt(get("paid")).lower()
        if mentors_part and not paid:
            paid = "оплачено"
        if paid.startswith("возврат"):
            paid = "возврат"
        if paid not in ("оплачено", "возврат"):
            continue
        prof = {k: _txt(get(k)) for k, _ in PROFILE if _txt(get(k))}
        ph, nk = phone_key(get("phone")), nick(get("account"))
        # сначала по имени; по телефону — только если имя похоже (у супругов бывает один номер: Трегубовы, 02.11)
        p = conn.execute("SELECT * FROM people WHERE flow=? AND name=?", (f["name"], name)).fetchone()
        if not p and ph:
            same = conn.execute("SELECT * FROM people WHERE flow=? AND phone=?", (f["name"], ph)).fetchone()
            words = lambda x: {w for w in re.split(r"\W+", (x or "").lower().replace("ё", "е")) if len(w) > 2}
            if same and len(words(same["name"]) & words(name)) >= 2:
                p = same
        status = "в потоке" if paid == "оплачено" else "выбыл"
        if not p:
            if status != "в потоке":
                continue
            pid = int(conn.execute(
                "INSERT INTO people (flow, name, status, username, phone, profile, source, synced_at) "
                "VALUES (?,?,?,?,?,?, 'платформа', datetime('now'))",
                (f["name"], name, status, nk, ph, json.dumps(prof, ensure_ascii=False))).lastrowid)
            added += 1
        else:
            pid = p["id"]
            conn.execute("UPDATE people SET name=?, status=CASE WHEN moved_to IS NULL THEN ? ELSE status END, "
                         "username=COALESCE(username, ?), phone=COALESCE(?, phone), profile=?, synced_at=datetime('now') "
                         "WHERE id=?", (name, status, nk, ph, json.dumps(prof, ensure_ascii=False), pid))
            updated += 1
            left += status == "выбыл"
        if n in pics and not photo_path(pid).exists():
            try:
                save_photo(conn, pid, pics[n]._data())
            except Exception:
                pass
    msg = f"из таблицы: новых {added}, обновлено {updated}" + (f", возвратов {left}" if left else "")
    conn.execute("INSERT OR REPLACE INTO flow_sync (flow, at, ok, message) VALUES (?, datetime('now'), 1, ?)",
                 (f["name"], msg))
    return msg


def sync_all(conn) -> list[str]:
    out = []
    for f in conn.execute("SELECT f.* FROM flows f JOIN settings s ON s.key='fourday:sheet:' || f.id "
                          "WHERE s.value<>'' AND f.status<>'завершён'").fetchall():
        try:
            out.append(f"{f['name']}: {sync(conn, f['id'])}")
        except Exception as e:
            conn.execute("INSERT OR REPLACE INTO flow_sync (flow, at, ok, message) VALUES (?, datetime('now'), 0, ?)",
                         (f["name"], f"не удалось прочитать таблицу участников: {e}"[:400]))
    return out


def start_background() -> None:
    """Таблица участников — раз в 15 минут (Лилия дописывает оплативших)."""
    import threading
    import time

    def loop():
        while True:
            time.sleep(60)
            c = store.connect()
            try:
                sync_all(c)
            finally:
                c.close()
            time.sleep(14 * 60)
    threading.Thread(target=loop, daemon=True, name="fourday-sync").start()


# ----------------------------------------------------------------- список и переход в ММ8

def participants(conn, f) -> list[dict]:
    from .roster import card_id
    rows = []
    for p in conn.execute("SELECT * FROM people WHERE flow=? ORDER BY status<>'в потоке', name", (f["name"],)):
        has_review = conn.execute("SELECT status FROM reviews WHERE tg_user_id=? ORDER BY id DESC LIMIT 1",
                                  (card_id(p["id"]),)).fetchone()
        sign = decl.sign_state(conn, p["id"])["state"] if p["source"] == "платформа" else ""
        moved = conn.execute("SELECT p.flow, t.title FROM people p LEFT JOIN teams t ON t.id=p.team_id WHERE p.id=?",
                             (p["moved_to"],)).fetchone() if p["moved_to"] else None
        rows.append(dict(p, card=card_id(p["id"]), review=has_review["status"] if has_review else None,
                         sign=sign, moved=dict(moved) if moved else None))
    return rows


def set_next(conn, pid: int, value: str, who: str = "") -> None:
    if value not in NEXT + ("",):
        raise ValueError("Неизвестный ответ.")
    conn.execute("UPDATE people SET next_flow=?, next_at=datetime('now'), next_by=? WHERE id=?",
                 (value or None, who or None, pid))


def transfer(conn, pid: int, flow_team_id: int, who: str) -> int:
    """Перевести участника 4-дневного мастермайнда в основной поток: та же личность
    (телеграм, ник, телефон, фото, анкета) и та же декларация — цели, шаги,
    условия, подписанные версии. Карточка 4-дневного остаётся историей."""
    from .roster import card_id
    p = conn.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
    if not p:
        raise decl.DeclError("Участник не найден.")
    if p["moved_to"]:
        raise decl.DeclError(f"«{p['name']}» уже переведён.")
    t = conn.execute("SELECT * FROM flow_teams WHERE id=?", (flow_team_id,)).fetchone()
    if not t:
        raise decl.DeclError("Выберите команду основного потока.")
    tf = flows.get(conn, t["flow_id"])
    exist = conn.execute("SELECT id FROM people WHERE flow=? AND (name=? OR (tg_user_id IS NOT NULL AND tg_user_id=?))",
                         (tf["name"], p["name"], p["tg_user_id"])).fetchone()
    if exist:
        raise decl.DeclError(f"В потоке {tf['name']} уже есть «{p['name']}».")
    new = decl.add_person(conn, flow_team_id, p["name"], p["username"] or "", 0)
    conn.execute("UPDATE people SET tg_user_id=?, phone=?, profile=?, word_price=?, perk=?, money_goal=?, moved_from=? "
                 "WHERE id=?", (p["tg_user_id"], p["phone"], p["profile"], p["word_price"], p["perk"], p["money_goal"],
                                pid, new))
    if photo_path(pid).exists():
        PHOTOS.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(photo_path(pid), photo_path(new))
        conn.execute("UPDATE people SET photo=? WHERE id=?", (f"{new}.jpg", new))
    # декларация: цели и шаги (отметки «сделано» — нет, поток начинается заново)
    for g in conn.execute("SELECT * FROM decl_goals WHERE person_id=? ORDER BY no", (pid,)).fetchall():
        gid = int(conn.execute("INSERT INTO decl_goals (person_id, no, title, deadline, progress, proof) VALUES (?,?,?,?,0,?)",
                               (new, g["no"], g["title"], g["deadline"], g["proof"])).lastrowid)
        for s in conn.execute("SELECT * FROM decl_steps WHERE goal_id=? ORDER BY id", (g["id"],)):
            conn.execute("INSERT INTO decl_steps (goal_id, person_id, title, deadline, done) VALUES (?,?,?,?,0)",
                         (gid, new, s["title"], s["deadline"]))
    planka = decl.planka_of(conn, pid)
    if planka:
        decl.set_planka(conn, new, planka)
    for v in conn.execute("SELECT * FROM decl_versions WHERE person_id=? ORDER BY no", (pid,)).fetchall():
        conn.execute("INSERT INTO decl_versions (person_id, no, content, hash, status, created_by, created_at, signed_by, signed_on) "
                     "VALUES (?,?,?,?,?,?,?,?,?)", (new, v["no"], v["content"], v["hash"], v["status"], v["created_by"],
                                                    v["created_at"], v["signed_by"], v["signed_on"]))
    # разборы — те же записи видны и в новой карточке
    conn.execute("UPDATE reviews SET tg_user_id=? WHERE tg_user_id=?", (card_id(new), card_id(pid)))
    conn.execute("UPDATE people SET moved_to=?, next_flow='да' WHERE id=?", (new, pid))
    # учётка участника теперь открывает карточку основного потока
    conn.execute("UPDATE users SET tg_user_id=? WHERE role='participant' AND tg_user_id=?", (card_id(new), card_id(pid)))
    decl.recount(conn, new)
    return new


# ----------------------------------------------------------------- регистрация

def match(conn, flow_name: str, username: str | None = None, phone: str | None = None) -> sqlite3.Row | None:
    """Карточка участника, которую узнали по @нику или телефону (ещё без телеграма)."""
    if username:
        p = conn.execute("SELECT * FROM people WHERE flow=? AND tg_user_id IS NULL AND status='в потоке' "
                         "AND lower(username)=?", (flow_name, username.lower().lstrip("@"))).fetchone()
        if p:
            return p
    key = phone_key(phone)
    if key:
        return conn.execute("SELECT * FROM people WHERE flow=? AND tg_user_id IS NULL AND status='в потоке' AND phone=?",
                            (flow_name, key)).fetchone()
    return None


def profile(p) -> list[tuple[str, str]]:
    try:
        d = json.loads(p["profile"] or "{}")
    except ValueError:
        d = {}
    return [(label, d[k]) for k, label in PROFILE if d.get(k)]
