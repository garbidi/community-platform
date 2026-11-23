"""Очередь долгих задач.

Зачем. Помощник работает внутри `claude -p`, а оттуда нельзя отцепить
работу: фоновые процессы гибнут вместе с ходом, а флаг фонового запуска в
этом режиме отвергается. Поэтому на просьбу «запусти фоном» помощник
раньше честно отвечал «запускаю» и не делал ничего — инструментов он при
этом не вызывал вовсе.

Отцепиться можно только снаружи. Здесь и есть это «снаружи»: помощник
кладёт задачу в очередь одной командой и сразу заканчивает ход, а
отдельный работник в процессе бота выполняет её своим ходом и присылает
результат в чат отдельным сообщением.

Каждая задача идёт в своей сессии: две параллельные работы в одной
сессии ведут себя непредсказуемо.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import store

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id       INTEGER PRIMARY KEY,
    chat     TEXT NOT NULL,
    task     TEXT NOT NULL,
    state    TEXT NOT NULL DEFAULT 'ждёт',   -- ждёт | идёт | готово | сбой
    result   TEXT,
    created  TEXT NOT NULL DEFAULT (datetime('now')),
    started  TEXT,
    finished TEXT
);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, id);
"""


def init() -> None:
    conn = store.connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def add(chat: str, task: str) -> int:
    init()
    conn = store.connect()
    try:
        cur = conn.execute("INSERT INTO jobs (chat, task) VALUES (?,?)",
                           (str(chat), task.strip()))
        return int(cur.lastrowid)
    finally:
        conn.close()


def revive() -> int:
    """Возвращает в очередь задачи, застрявшие в «идёт».

    Если работник упал или бота перезапустили посреди работы, задача так
    и осталась бы помеченной идущей и не досталась бы никому.
    """
    init()
    conn = store.connect()
    try:
        cur = conn.execute("UPDATE jobs SET state='ждёт', started=NULL "
                           "WHERE state='идёт'")
        return cur.rowcount or 0
    finally:
        conn.close()


def take() -> sqlite3.Row | None:
    """Забирает одну ждущую задачу и помечает её идущей.

    Пометка и выборка одним запросом: иначе два работника могут взять
    одну и ту же задачу.
    """
    init()
    conn = store.connect()
    try:
        row = conn.execute(
            "UPDATE jobs SET state='идёт', started=datetime('now') "
            "WHERE id = (SELECT id FROM jobs WHERE state='ждёт' "
            "            ORDER BY id LIMIT 1) RETURNING *").fetchone()
        return row
    except sqlite3.OperationalError:
        # RETURNING есть не во всех сборках sqlite — тогда в два приёма.
        row = conn.execute("SELECT * FROM jobs WHERE state='ждёт' "
                           "ORDER BY id LIMIT 1").fetchone()
        if row is None:
            return None
        conn.execute("UPDATE jobs SET state='идёт', started=datetime('now') "
                     "WHERE id=? AND state='ждёт'", (row["id"],))
        return row
    finally:
        conn.close()


def finish(job_id: int, ok: bool, result: str) -> None:
    conn = store.connect()
    try:
        conn.execute("UPDATE jobs SET state=?, result=?, "
                     "finished=datetime('now') WHERE id=?",
                     ("готово" if ok else "сбой", result[:20000], job_id))
    finally:
        conn.close()


def waiting(chat: str) -> list[dict[str, Any]]:
    init()
    conn = store.connect()
    try:
        rows = conn.execute(
            "SELECT id, task, state, created FROM jobs "
            "WHERE chat=? AND state IN ('ждёт','идёт') ORDER BY id",
            (str(chat),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
