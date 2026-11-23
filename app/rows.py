"""Таблица потока: строки участников, как в Google-таблице организаторов, но на платформе.

Строка — это участник потока (people): у неё та же карточка, разборы и декларация.
Сверху к строке добавлены комментарии, теги и оплаты.

Откуда берутся строки:
  • оплата с Тильды или по безналу (sens_orders) → строка в ближайшем потоке,
    который ещё не начался; если такого потока нет — оплата ждёт во «Входящих»;
  • вручную — кнопкой «Добавить строку».
Перенос между потоками — в любой созданный, не завершённый поток. В старом потоке
остаётся строка-след «перенесён в …», всё остальное (комментарии, теги, оплаты,
разборы, цели) переезжает с человеком.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from datetime import date
from typing import Any

from . import flows, store

SCHEMA = """
CREATE TABLE IF NOT EXISTS row_notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id   INTEGER NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'комментарий',   -- комментарий | событие
    text        TEXT NOT NULL,
    author      TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_row_notes ON row_notes(person_id, id);

CREATE TABLE IF NOT EXISTS row_tags (
    person_id   INTEGER NOT NULL,
    tag         TEXT NOT NULL,
    PRIMARY KEY (person_id, tag)
);

CREATE TABLE IF NOT EXISTS row_pays (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id   INTEGER,                 -- NULL — ждёт во «Входящих»
    order_id    INTEGER UNIQUE,          -- sens_orders.id; NULL — внесено вручную
    amount      INTEGER,
    paid_on     TEXT,                    -- ГГГГ-ММ-ДД
    kind        TEXT,                    -- карта | безнал | вручную
    product     TEXT,
    name        TEXT, phone TEXT, tg TEXT, city TEXT, engaged TEXT,
    status      TEXT NOT NULL DEFAULT 'ждёт',   -- ждёт | в строке | пропущено
    note        TEXT,
    added_by    TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_row_pays ON row_pays(person_id);
"""

# оплаты Тильды и безнала берём начиная с этой даты: всё раньше — за ММ8, оно уже в таблице Лилии
ORDERS_FROM = "2026-11-09"

# столбцы таблицы: ключ, заголовок, деньги ли (доход и прибыль видят только администраторы).
# name, phone, username — поля people; остальное — в people.profile (те же ключи, что у анкеты 4 дней).
COLUMNS = [
    ("name", "Имя Фамилия", False),
    ("phone", "Телефон", False),
    ("username", "Телеграм", False),
    ("birthday", "Дата рождения", False),
    ("city", "Город", False),
    ("inviter", "Кто вовлёк", False),
    ("mentor", "Ментор", False),
    ("profit", "Чистая прибыль", True),
    ("revenue", "Доход", True),
    ("niche", "Ниша", False),
    ("point_a", "Точка А", False),
    ("point_b", "Точка Б", False),
    ("motives", "Истинные мотивы", False),
    ("expect", "Ожидания", False),
    ("comment", "Пометки", False),
]
KEYS = {k for k, _, _ in COLUMNS}
MONEY = {k for k, _, m in COLUMNS if m}
# что разбор записи предлагает в карточку (person_facts) → в какой столбец ложится
FROM_FACTS = {"city": "city", "work": "niche", "income": "revenue", "request": "point_b"}

MOVED = "перенесён"


class RowError(Exception):
    pass


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


# ----------------------------------------------------------------- общее

def phone_key(v) -> str | None:
    d = re.sub(r"\D", "", str(v or ""))
    return d[-10:] if len(d) >= 10 else None


def fmt_phone(d: str | None) -> str:
    """9161234567 → +7 916 123-45-67."""
    return f"+7 {d[:3]} {d[3:6]}-{d[6:8]}-{d[8:]}" if d and len(d) == 10 else (d or "")


def nick(v) -> str | None:
    s = re.sub(r"^(https?://)?t\.me/", "", str(v or "").strip()).lstrip("@").strip()
    return s if re.fullmatch(r"[A-Za-z0-9_]{4,32}", s) else None


def _profile(p) -> dict:
    try:
        d = json.loads(p["profile"] or "{}")
    except (ValueError, TypeError):
        d = {}
    return d if isinstance(d, dict) else {}


def _clean_name(raw: str | None) -> str:
    """«Гусева Лейла Ринатовна», «Елена  Комарова» → без отчества и лишних пробелов."""
    t = re.sub(r"\(.*?\)", " ", raw or "").split(",")[0]
    words = [w for w in t.split() if not re.search(r"(?i)(вич|вна|вны|ична|инична)$", w)]
    return " ".join(words)[:80].strip()


def open_flows(conn) -> list[sqlite3.Row]:
    """Потоки, в которые можно положить строку: созданные и не завершённые."""
    return conn.execute("SELECT * FROM flows WHERE status<>'завершён' "
                        "ORDER BY COALESCE(start, '9999'), id").fetchall()


def target_flow(conn) -> sqlite3.Row | None:
    """Куда класть новую оплату: ближайший поток, который ещё не начался (есть дата старта)."""
    return conn.execute("SELECT * FROM flows WHERE status='готовится' AND start > ? "
                        "ORDER BY start, id LIMIT 1", (date.today().isoformat(),)).fetchone()


def note(conn, pid: int, text: str, author: str | None, kind: str = "комментарий") -> None:
    text = (text or "").strip()
    if text:
        conn.execute("INSERT INTO row_notes (person_id, kind, text, author) VALUES (?,?,?,?)",
                     (pid, kind, text[:4000], author))


# ----------------------------------------------------------------- строки

def _new_person(conn, flow_name: str, name: str, phone: str = "", username: str = "",
                profile: dict | None = None) -> int:
    name = _clean_name(name)
    if not name:
        raise RowError("Укажите имя и фамилию.")
    base, n = name, 2
    while conn.execute("SELECT 1 FROM people WHERE flow=? AND name=?", (flow_name, name)).fetchone():
        name = f"{base} ({n})"
        n += 1
    return int(conn.execute(
        "INSERT INTO people (flow, name, status, username, phone, profile, source, synced_at) "
        "VALUES (?,?, 'в потоке', ?, ?, ?, 'платформа', datetime('now'))",
        (flow_name, name, nick(username), phone_key(phone),
         json.dumps(profile or {}, ensure_ascii=False))).lastrowid)


def find_same(conn, flow_name: str, phone: str | None, username: str | None) -> sqlite3.Row | None:
    """Тот же человек в потоке: по телефону, иначе по нику."""
    ph, nk = phone_key(phone), nick(username)
    if ph:
        r = conn.execute("SELECT * FROM people WHERE flow=? AND phone=? AND moved_to IS NULL", (flow_name, ph)).fetchone()
        if r:
            return r
    if nk:
        return conn.execute("SELECT * FROM people WHERE flow=? AND lower(username)=lower(?) AND moved_to IS NULL",
                            (flow_name, nk)).fetchone()
    return None


def add(conn, flow_id: int, name: str, phone: str, username: str, inviter: str, who: str) -> int:
    f = flows.get(conn, flow_id)
    if not f or f["status"] == "завершён":
        raise RowError("Выберите поток, который ещё не завершён.")
    same = find_same(conn, f["name"], phone, username)
    if same:
        raise RowError(f"В {f['name']} уже есть строка с этим телефоном или ником: «{same['name']}».")
    pid = _new_person(conn, f["name"], name, phone, username,
                      {"inviter": inviter.strip()} if inviter.strip() else {})
    note(conn, pid, "строка добавлена вручную", who, "событие")
    return pid


def set_cell(conn, pid: int, key: str, value: str, who: str, money_ok: bool) -> str:
    """Правка ячейки. Возвращает значение, как оно сохранилось."""
    p = conn.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
    if not p:
        raise RowError("Строка не найдена.")
    if key not in KEYS:
        raise RowError("Нет такого столбца.")
    if key in MONEY and not money_ok:
        raise RowError("Доход и прибыль правят только администраторы.")
    value = (value or "").strip()[:4000]
    if key == "name":
        name = _clean_name(value) if value else ""
        if not name:
            raise RowError("Имя не может быть пустым.")
        if name != p["name"] and conn.execute("SELECT 1 FROM people WHERE flow=? AND name=?",
                                              (p["flow"], name)).fetchone():
            raise RowError(f"«{name}» в {p['flow']} уже есть.")
        conn.execute("UPDATE people SET name=? WHERE id=?", (name, pid))
        return name
    if key == "phone":
        ph = phone_key(value)
        if value and not ph:
            raise RowError("В телефоне должно быть 10–11 цифр.")
        conn.execute("UPDATE people SET phone=? WHERE id=?", (ph, pid))
        return fmt_phone(ph)
    if key == "username":
        nk = nick(value)
        if value and not nk:
            raise RowError("Ник — латиница, цифры и _, от 4 знаков. Можно с @ или ссылкой t.me.")
        conn.execute("UPDATE people SET username=? WHERE id=?", (nk, pid))
        return nk or ""
    d = _profile(p)
    if value:
        d[key] = value
    else:
        d.pop(key, None)
    conn.execute("UPDATE people SET profile=? WHERE id=?", (json.dumps(d, ensure_ascii=False), pid))
    return value


def set_tags(conn, pid: int, raw: str) -> list[str]:
    tags = []
    for t in re.split(r"[,;\n]+", raw or ""):
        t = t.strip().lstrip("#").strip()[:40]
        if t and t.lower() not in (x.lower() for x in tags):
            tags.append(t)
    conn.execute("DELETE FROM row_tags WHERE person_id=?", (pid,))
    conn.executemany("INSERT INTO row_tags (person_id, tag) VALUES (?,?)", [(pid, t) for t in tags])
    return tags


UNKNOWN = "Кто вовлёк — уточнить"


def _words(name: str) -> frozenset:
    return frozenset(w for w in re.split(r"[\s.,;()«»\"']+", (name or "").lower().replace("ё", "е")) if len(w) > 1)


def inviters(conn) -> tuple[dict, dict]:
    """Кого платформа знает: участники всех потоков и организаторы/менторы. → (слова имени → имя, ник → имя)."""
    names, nicks = {}, {}
    for r in conn.execute("SELECT name, username FROM people WHERE moved_to IS NULL "
                          "UNION ALL SELECT name, login FROM users WHERE active=1"):
        w = _words(_clean_name(r[0]))
        if len(w) >= 2:
            names.setdefault(w, " ".join(_clean_name(r[0]).split()))
        if r[1]:
            nicks.setdefault(r[1].lower().lstrip("@"), " ".join(_clean_name(r[0]).split()))
    return names, nicks


def who_invited(text: str, known: tuple[dict, dict]) -> str | None:
    """«Михаил зейтунян», «Вяткин  Алексей», «@nick» → имя, как оно записано на платформе; «Мама», «Психолог» → None."""
    names, nicks = known
    raw = (text or "").strip()
    if not raw:
        return None
    nk = nick(raw)
    if nk and nk.lower() in nicks:
        return nicks[nk.lower()]
    w = _words(_clean_name(raw))
    if len(w) < 2:
        return None
    if w in names:
        return names[w]
    hits = {v for k, v in names.items() if w <= k}
    return hits.pop() if len(hits) == 1 else None


def table(conn, f, money_ok: bool, tag: str = "", q: str = "") -> list[dict]:
    """Строки потока для страницы. Перенесённые — в конце, серым."""
    from .roster import card_id
    ids = [r["id"] for r in conn.execute("SELECT id FROM people WHERE flow=?", (f["name"],))]
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    tags: dict[int, list[str]] = {}
    for r in conn.execute(f"SELECT * FROM row_tags WHERE person_id IN ({marks}) ORDER BY tag", ids):
        tags.setdefault(r["person_id"], []).append(r["tag"])
    notes = {r[0]: r[1] for r in conn.execute(
        f"SELECT person_id, COUNT(*) FROM row_notes WHERE kind='комментарий' AND person_id IN ({marks}) "
        f"GROUP BY person_id", ids)}
    pays = {r[0]: (r[1], r[2]) for r in conn.execute(
        f"SELECT person_id, SUM(COALESCE(amount,0)), COUNT(*) FROM row_pays WHERE status='в строке' "
        f"AND person_id IN ({marks}) GROUP BY person_id", ids)}
    cards = [card_id(i) for i in ids]
    cm = ",".join("?" * len(cards))
    facts: dict[int, dict] = {}
    for r in conn.execute(f"SELECT card, field, value, status FROM person_facts WHERE card IN ({cm}) "
                          f"AND status IN ('подтверждено','предложено') ORDER BY created_at, id", cards):
        x = facts.setdefault(-r["card"], {"ok": {}, "new": 0})
        if r["status"] == "подтверждено":
            x["ok"][r["field"]] = r["value"]
        elif r["field"] not in MONEY_FACTS or money_ok:
            x["new"] += 1
    revs = {-r[0]: r[1] for r in conn.execute(
        f"SELECT tg_user_id, status FROM reviews WHERE tg_user_id IN ({cm}) ORDER BY id", cards)}
    moved = {r["id"]: r["flow"] for r in conn.execute(
        "SELECT id, flow FROM people WHERE id IN (SELECT moved_to FROM people WHERE flow=? AND moved_to IS NOT NULL)",
        (f["name"],))}
    known = inviters(conn)
    out = []
    for p in conn.execute(f"SELECT * FROM people WHERE id IN ({marks})", ids):
        d = _profile(p)
        fx = facts.get(p["id"], {"ok": {}, "new": 0})
        cells = {}
        for k, _, money in COLUMNS:
            if money and not money_ok:
                continue
            if k in ("name", "phone", "username"):
                cells[k] = {"v": fmt_phone(p[k]) if k == "phone" else (p[k] or ""), "auto": False}
                continue
            v, auto = d.get(k) or "", False
            if not v:
                src = next((fk for fk, col in FROM_FACTS.items() if col == k), None)
                if src and fx["ok"].get(src):
                    v, auto = fx["ok"][src], True
            cells[k] = {"v": str(v), "auto": auto}
        row = {"id": p["id"], "card": card_id(p["id"]), "name": p["name"], "status": p["status"],
               "moved_to": moved.get(p["moved_to"]) if p["moved_to"] else None,
               "moved_from": p["moved_from"], "cells": cells, "tags": tags.get(p["id"], []),
               "notes": notes.get(p["id"], 0), "paid": pays.get(p["id"], (0, 0)),
               "proposed": fx["new"], "review": revs.get(p["id"]),
               "inviter": (d.get("inviter") or "").strip()}
        row["group"] = who_invited(row["inviter"], known) or UNKNOWN
        if "inviter" in cells and row["inviter"] and row["group"] == UNKNOWN:
            cells["inviter"]["warn"] = True
        if tag and tag not in row["tags"]:
            continue
        if q and q.lower() not in json.dumps(row, ensure_ascii=False).lower():
            continue
        out.append(row)
    out.sort(key=lambda r: (r["moved_to"] is not None, r["status"] != "в потоке",
                            r["group"] == UNKNOWN, r["group"].lower(), r["name"].lower()))
    return out


MONEY_FACTS = {"income"}


def flow_tags(conn, f) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT t.tag FROM row_tags t JOIN people p ON p.id=t.person_id WHERE p.flow=? ORDER BY t.tag",
        (f["name"],))]


def row(conn, pid: int) -> dict | None:
    from .roster import card_id
    p = conn.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
    if not p:
        return None
    return {
        "p": p, "card": card_id(pid), "profile": _profile(p),
        "flow": flows.get(conn, name=p["flow"]),
        "tags": [r[0] for r in conn.execute("SELECT tag FROM row_tags WHERE person_id=? ORDER BY tag", (pid,))],
        "notes": conn.execute("SELECT * FROM row_notes WHERE person_id=? ORDER BY id DESC", (pid,)).fetchall(),
        "pays": conn.execute("SELECT * FROM row_pays WHERE person_id=? ORDER BY COALESCE(paid_on, created_at), id",
                             (pid,)).fetchall(),
        "reviews": conn.execute("SELECT id, title, held_on, status, source_name, created_at FROM reviews "
                                "WHERE tg_user_id=? ORDER BY id DESC", (card_id(pid),)).fetchall(),
        "moved_to": conn.execute("SELECT id, flow FROM people WHERE id=?", (p["moved_to"],)).fetchone()
                    if p["moved_to"] else None,
        "moved_from": conn.execute("SELECT id, flow FROM people WHERE id=?", (p["moved_from"],)).fetchone()
                      if p["moved_from"] else None,
    }


# ----------------------------------------------------------------- перенос

def move(conn, pid: int, flow_id: int, who: str) -> int:
    """Перенести строку в другой поток. Возвращает id новой строки."""
    from .roster import card_id
    from .fourday import photo_path, PHOTOS
    p = conn.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
    if not p:
        raise RowError("Строка не найдена.")
    if p["moved_to"]:
        raise RowError(f"«{p['name']}» уже перенесён.")
    f = flows.get(conn, flow_id)
    if not f or f["status"] == "завершён":
        raise RowError("Выберите поток, который ещё не завершён. Если нужного нет — сначала создайте его в «Потоках».")
    if f["name"] == p["flow"]:
        raise RowError(f"Строка и так в {f['name']}.")
    same = find_same(conn, f["name"], p["phone"], p["username"]) or conn.execute(
        "SELECT * FROM people WHERE flow=? AND name=?", (f["name"], p["name"])).fetchone()
    if same:
        raise RowError(f"В {f['name']} уже есть «{same['name']}» — проверьте, не тот же ли это человек.")
    new = int(conn.execute(
        "INSERT INTO people (flow, name, status, tg_user_id, username, phone, profile, word_price, perk, "
        "money_goal, source, synced_at, moved_from) VALUES (?,?, 'в потоке', ?,?,?,?,?,?,?, 'платформа', datetime('now'), ?)",
        (f["name"], p["name"], p["tg_user_id"], p["username"], p["phone"], p["profile"], p["word_price"],
         p["perk"], p["money_goal"], pid)).lastrowid)
    if p["photo"] and photo_path(pid).exists():
        PHOTOS.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(photo_path(pid), photo_path(new))
        conn.execute("UPDATE people SET photo=? WHERE id=?", (f"{new}.jpg", new))
    # всё, что было у строки, едет с человеком
    for t in ("row_notes", "row_tags", "row_pays", "decl_goals", "decl_steps"):
        conn.execute(f"UPDATE {t} SET person_id=? WHERE person_id=?", (new, pid))
    for t, col in (("reviews", "tg_user_id"), ("person_facts", "card")):
        conn.execute(f"UPDATE {t} SET {col}=? WHERE {col}=?", (card_id(new), card_id(pid)))
    conn.execute("UPDATE users SET tg_user_id=? WHERE role IN ('participant','participant_plus') AND tg_user_id=?",
                 (card_id(new), card_id(pid)))
    conn.execute("UPDATE people SET moved_to=?, status=? WHERE id=?", (new, MOVED, pid))
    note(conn, new, f"перенесён из {p['flow']} в {f['name']}", who, "событие")
    note(conn, pid, f"перенесён в {f['name']}", who, "событие")
    return new


# ----------------------------------------------------------------- оплаты

def pull(conn) -> int:
    """Новые оплаты из Тильды и безнала → «Входящие» → строки ближайшего потока."""
    since = store.get_setting(conn, "rows:orders_from") or ORDERS_FROM
    new = 0
    for o in conn.execute(
            "SELECT * FROM sens_orders WHERE substr(at,1,10) >= ? AND status<>'архив' "
            "AND (kind='карта' OR paid_at IS NOT NULL) "
            "AND id NOT IN (SELECT order_id FROM row_pays WHERE order_id IS NOT NULL) ORDER BY id",
            (since,)).fetchall():
        conn.execute(
            "INSERT INTO row_pays (order_id, amount, paid_on, kind, product, name, phone, tg, city, engaged, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?, 'ждёт')",
            (o["id"], o["amount"], str(o["paid_at"] or o["at"] or "")[:10] or None, o["kind"], o["product"],
             o["name"], o["phone"], o["tg"], o["city"], o["engaged"]))
        new += 1
    f = target_flow(conn)
    if f:
        for pay in conn.execute("SELECT id FROM row_pays WHERE status='ждёт' AND person_id IS NULL ORDER BY id").fetchall():
            place(conn, pay["id"], f["id"], "платформа")
    return new


def inbox(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM row_pays WHERE status='ждёт' AND person_id IS NULL ORDER BY id").fetchall()


def place(conn, pay_id: int, flow_id: int, who: str, pid: int | None = None) -> int:
    """Оплату — в строку потока: в указанную, в строку того же человека (телефон, ник) или в новую."""
    pay = conn.execute("SELECT * FROM row_pays WHERE id=?", (pay_id,)).fetchone()
    if not pay or pay["person_id"]:
        raise RowError("Эта оплата уже разобрана.")
    f = flows.get(conn, flow_id)
    if not f or f["status"] == "завершён":
        raise RowError("Выберите поток, который ещё не завершён.")
    what = f"оплата {pay['amount'] or '?'} ₽ · {pay['kind']} · {pay['paid_on'] or ''}".strip(" ·")
    if pid:
        target = conn.execute("SELECT * FROM people WHERE id=? AND flow=?", (pid, f["name"])).fetchone()
        if not target:
            raise RowError("Строка не найдена в этом потоке.")
    else:
        target = find_same(conn, f["name"], pay["phone"], pay["tg"])
    if target:
        pid = target["id"]
        d = _profile(target)
        changed = False
        for k, v in (("city", pay["city"]), ("inviter", pay["engaged"])):
            if v and not d.get(k):
                d[k] = v.strip(); changed = True
        if changed:
            conn.execute("UPDATE people SET profile=? WHERE id=?", (json.dumps(d, ensure_ascii=False), pid))
        if pay["phone"] and not target["phone"]:
            conn.execute("UPDATE people SET phone=? WHERE id=?", (phone_key(pay["phone"]), pid))
        if pay["tg"] and not target["username"] and nick(pay["tg"]):
            conn.execute("UPDATE people SET username=? WHERE id=?", (nick(pay["tg"]), pid))
        note(conn, pid, what, who, "событие")
    else:
        prof = {k: v.strip() for k, v in (("city", pay["city"]), ("inviter", pay["engaged"])) if v and v.strip()}
        pid = _new_person(conn, f["name"], pay["name"] or "Без имени", pay["phone"] or "", pay["tg"] or "", prof)
        note(conn, pid, "строка создана оплатой: " + what, who, "событие")
    conn.execute("UPDATE row_pays SET person_id=?, status='в строке' WHERE id=?", (pid, pay_id))
    return pid


def skip(conn, pay_id: int, who: str, why: str = "") -> None:
    conn.execute("UPDATE row_pays SET status='пропущено', note=?, added_by=? WHERE id=? AND person_id IS NULL",
                 ((why or "").strip()[:300] or None, who, pay_id))


def add_pay(conn, pid: int, amount: str, paid_on: str, text: str, who: str) -> None:
    """Оплата, внесённая руками: наличные, перевод, рассрочка."""
    try:
        n = int(re.sub(r"\D", "", amount or ""))
    except ValueError:
        raise RowError("Сумма — числом, в рублях.")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", paid_on or ""):
        paid_on = date.today().isoformat()
    conn.execute("INSERT INTO row_pays (person_id, amount, paid_on, kind, status, note, added_by) "
                 "VALUES (?,?,?, 'вручную', 'в строке', ?, ?)", (pid, n, paid_on, (text or "").strip()[:300] or None, who))
    note(conn, pid, f"оплата {n} ₽ внесена вручную" + (f": {text.strip()}" if text and text.strip() else ""), who, "событие")
