"""Сведения об участнике потока: кто он, контакты.

Каждое сведение — отдельная запись с источником: кто или что его внесло
(ментор руками, созвон из Стенографа) и когда. Источники бывают двух
сортов: подтверждённые (видны как факт) и предложенные — их найдёт разбор
созвонов, и ментор примет или отклонит. Так машина не перепишет молча то,
что человек уже знает лучше: разметка голосов в записи ошибается, и чужая
фраза не должна стать чужим доходом.
"""

from __future__ import annotations

import sqlite3
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS person_facts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    card        INTEGER NOT NULL,              -- номер карточки (у нового потока отрицательный)
    field       TEXT NOT NULL,
    value       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'подтверждено',   -- подтверждено | предложено | отклонено
    source      TEXT NOT NULL,                 -- «вписал ментор», «созвон 09.10 — Разбор»
    source_ref  TEXT,                          -- id созвона, отрезок записи
    quote       TEXT,                          -- откуда взято: фраза из созвона
    author      TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_facts_card ON person_facts(card, field, status);
"""

# Порядок — как читает ментор перед встречей: сначала кто это, потом как связаться.
FIELDS = [
    ("city", "Город", "who"),
    ("work", "Чем занимается", "who"),
    ("income", "Доход", "who"),
    ("request", "Запрос на поток", "who"),
    ("about", "О себе", "who"),
    ("telegram", "Телеграм", "contacts"),
    ("phone", "Телефон", "contacts"),
]
LABELS = {k: t for k, t, _ in FIELDS}
# Доход — про деньги, а деньги ментору не положены (как и в остальной карточке)
MONEY_FIELDS = {"income"}


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def facts(conn: sqlite3.Connection, card: int, money: bool) -> dict[str, Any]:
    """Текущее значение каждого поля, его история и предложения из созвонов."""
    rows = conn.execute(
        "SELECT * FROM person_facts WHERE card=? ORDER BY created_at DESC, id DESC",
        (card,)).fetchall()
    out = {}
    for key, title, group in FIELDS:
        if key in MONEY_FIELDS and not money:
            continue
        mine = [r for r in rows if r["field"] == key]
        current = next((r for r in mine if r["status"] == "подтверждено"), None)
        out[key] = {
            "key": key, "title": title, "group": group, "current": current,
            "proposed": [r for r in mine if r["status"] == "предложено"],
            "history": [r for r in mine if r["status"] == "подтверждено"][1:],
        }
    return out


def set_fact(conn: sqlite3.Connection, card: int, field: str, value: str, author: str) -> None:
    if field not in LABELS:
        raise ValueError(field)
    value = value.strip()
    cur = conn.execute(
        "SELECT value FROM person_facts WHERE card=? AND field=? AND status='подтверждено' "
        "ORDER BY created_at DESC, id DESC LIMIT 1", (card, field)).fetchone()
    if (cur["value"] if cur else "") == value:
        return
    conn.execute("INSERT INTO person_facts (card, field, value, source, author) "
                 "VALUES (?,?,?,?,?)", (card, field, value, "вписал вручную", author))


def propose(conn: sqlite3.Connection, card: int, found, source: str, ref: str) -> int:
    """Сведения из разбора — предложениями: {"city": {"value", "quote"}, …}.
    То, что уже стоит в карточке тем же значением, и повтор того же разбора или
    созвона (поле из него уже предлагали — принято или нет) не плодим."""
    if not isinstance(found, dict):
        return 0
    n = 0
    for key, f in found.items():
        if key not in LABELS or key in ("telegram", "phone") or not isinstance(f, dict):
            continue
        value = str(f.get("value") or "").strip()[:500]
        if not value or value.lower() in ("null", "не звучало"):
            continue
        same = conn.execute(
            "SELECT 1 FROM person_facts WHERE card=? AND field=? AND status<>'отклонено' "
            "AND (lower(value)=lower(?) OR source_ref=?)", (card, key, value, ref)).fetchone()
        if same:
            continue
        conn.execute("INSERT INTO person_facts (card, field, value, status, source, source_ref, quote) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (card, key, value, "предложено", source, ref, str(f.get("quote") or "").strip()[:300] or None))
        n += 1
    return n


def decide(conn: sqlite3.Connection, card: int, fact_id: int, accept: bool, author: str) -> None:
    """Принять или отклонить сведение, предложенное разбором созвона."""
    conn.execute(
        "UPDATE person_facts SET status=?, author=COALESCE(author,'') || ? "
        "WHERE id=? AND card=? AND status='предложено'",
        ("подтверждено" if accept else "отклонено", f" · {'принял' if accept else 'отклонил'} {author}",
         fact_id, card))
    if accept:
        # принятое становится свежим значением поля
        conn.execute("UPDATE person_facts SET created_at=datetime('now') WHERE id=?", (fact_id,))
