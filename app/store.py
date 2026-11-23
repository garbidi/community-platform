"""База приложения: пользователи, роли, сессии, привязки и журнал доступа.

База знаний (kb.sqlite) открывается только на чтение и здесь не меняется —
всё, что приложение записывает, живёт в отдельном файле.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .config import APP_DB, SESSION_DAYS

MSK = timezone(timedelta(hours=3))

ROLES = {
    "participant": "Участник",
    "participant_plus": "Участник · расширенный тариф",
    "mentor": "Ментор",
    "coordinator": "Координатор",
    "admin": "Администратор",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    login      TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    role       TEXT NOT NULL,
    tg_user_id INTEGER,             -- связь с участником в базе знаний
    salt       TEXT NOT NULL,
    pwhash     TEXT NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL
);

-- Кто из участников закреплён за ментором. Ментор видит только их.
CREATE TABLE IF NOT EXISTS assignments (
    mentor_id  INTEGER NOT NULL REFERENCES users(id),
    tg_user_id INTEGER NOT NULL,
    PRIMARY KEY (mentor_id, tg_user_id)
);

-- Журнал обращений к чужим профилям: требование к работе
-- с чувствительными данными, см. план, раздел 11.
CREATE TABLE IF NOT EXISTS access_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT NOT NULL DEFAULT (datetime('now')),
    user_id   INTEGER,
    login     TEXT,
    role      TEXT,
    action    TEXT NOT NULL,
    target    TEXT,
    detail    TEXT
);

CREATE INDEX IF NOT EXISTS idx_log_at ON access_log(at);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Разборы участников. Материал и промпт готовит приложение, ответ пишет модель
-- (сейчас — Claude Code через analyze.py). Провайдер меняется, таблица остаётся.
CREATE TABLE IF NOT EXISTS analyses (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_user_id     INTEGER NOT NULL,
    status         TEXT NOT NULL DEFAULT 'ожидает',   -- ожидает | готов | ошибка
    provider       TEXT NOT NULL DEFAULT 'queue',
    requested_by   INTEGER,
    requested_login TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at    TEXT,
    messages_used  INTEGER,
    material_chars INTEGER,
    prompt_path    TEXT,
    text           TEXT,
    error          TEXT
);

CREATE INDEX IF NOT EXISTS idx_analyses_user ON analyses(tg_user_id, id DESC);

-- Смысловая разметка реплик вторым слоем: её ставит модель, а не правила.
-- Ключ — первое сообщение склеенной реплики. Пересчёт склейки ключ не меняет,
-- пока не изменилась сама переписка.
CREATE TABLE IF NOT EXISTS reply_marks (
    chat_id    INTEGER NOT NULL,
    msg_id     INTEGER NOT NULL,
    label      TEXT NOT NULL,      -- знание | вопрос | обязательство | результат |
                                   -- личное | поддержка | организация | спор | болтовня
    usefulness INTEGER NOT NULL,   -- 0…3: насколько реплика полезна для разбора человека
    note       TEXT,
    model      TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (chat_id, msg_id)
);

-- Группы потока: команды, которые соревнуются между собой.
CREATE TABLE IF NOT EXISTS teams (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    flow       TEXT,                       -- ММ5, ММ6, ММ7…
    goal       INTEGER NOT NULL DEFAULT 0, -- вовлечений до победы
    note       TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    archived   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS team_members (
    team_id    INTEGER NOT NULL REFERENCES teams(id),
    tg_user_id INTEGER NOT NULL,
    role       TEXT NOT NULL DEFAULT 'участник',   -- участник | лидер | ментор
    PRIMARY KEY (team_id, tg_user_id)
);

-- Вовлечения. Пока заносятся руками; когда появится источник, который видит
-- приход денег, он будет писать сюда же — поля те же, добавится только origin.
CREATE TABLE IF NOT EXISTS involvements (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_user_id INTEGER NOT NULL,           -- кто вовлёк
    happened   TEXT NOT NULL,              -- дата, ГГГГ-ММ-ДД
    amount     INTEGER NOT NULL DEFAULT 0, -- сколько денег принесло, ₽
    who        TEXT,                       -- кого вовлёк
    status     TEXT NOT NULL DEFAULT 'оплачено',  -- оплачено | предоплата | ожидание
    note       TEXT,
    origin     TEXT NOT NULL DEFAULT 'вручную',   -- вручную | <название источника>
    source     TEXT,                       -- «чат/сообщение», если перенесено из объявления
    added_by   TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_inv_user ON involvements(tg_user_id, happened);

-- Движение денег помимо вовлечений: штрафы, возвраты, бонусы.
CREATE TABLE IF NOT EXISTS charges (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_user_id INTEGER NOT NULL,
    happened   TEXT NOT NULL,
    amount     INTEGER NOT NULL,           -- со знаком: штраф отрицательный
    kind       TEXT NOT NULL DEFAULT 'штраф',   -- штраф | возврат | бонус | прочее
    reason     TEXT,
    added_by   TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_charge_user ON charges(tg_user_id, happened);

-- Что участник рассказывает о себе сам: цели на мастермайнд, чем занимается,
-- какая помощь нужна от клуба. Это единственные данные, которые он вносит
-- своими руками, — и они же основа будущего поиска людей внутри клуба.
CREATE TABLE IF NOT EXISTS participant_profile (
    tg_user_id INTEGER PRIMARY KEY,
    goals      TEXT,
    about      TEXT,
    need       TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by TEXT
);

-- Поисковый индекс по корпусу. Лежит здесь, а не в базе знаний,
-- чтобы её можно было пересобрать сборщиком и не потерять ничего своего.
CREATE VIRTUAL TABLE IF NOT EXISTS corpus_fts USING fts5(
    text,
    chat_id   UNINDEXED,
    msg_id    UNINDEXED,
    user_id   UNINDEXED,
    date      UNINDEXED,
    kind      UNINDEXED,
    tokenize  = "unicode61 remove_diacritics 2"
);
"""


def connect() -> sqlite3.Connection:
    APP_DB.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None — запись фиксируется сразу, а не в конце запроса.
    # Иначе браузер успевает пойти по редиректу раньше, чем закоммитится
    # только что открытая сессия, и вход теряется через раз.
    # check_same_thread=False: FastAPI открывает подключение в одном потоке пула,
    # а запрос выполняет в другом; запрос пользуется им строго по очереди
    conn = sqlite3.connect(APP_DB, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Досыпать столбцы, появившиеся после первого выпуска.

    База живёт у заказчика и пересоздавать её нельзя: в ней уже есть
    пользователи, журнал и разборы.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(analyses)")}
    if "kind" not in cols:
        conn.execute("ALTER TABLE analyses ADD COLUMN kind TEXT NOT NULL "
                     "DEFAULT 'ментору'")

    # Откуда взялась запись о вовлечении: «чат/сообщение» для перенесённых
    # из объявлений. По этому ключу повторный перенос не создаёт дублей.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(involvements)")}
    if "source" not in cols:
        conn.execute("ALTER TABLE involvements ADD COLUMN source TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_inv_source "
                     "ON involvements(source)")


# ----------------------------------------------------------------- пароли

def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt),
                                 200_000)
    return salt, digest.hex()


def check_password(password: str, salt: str, pwhash: str) -> bool:
    _, candidate = hash_password(password, salt)
    return secrets.compare_digest(candidate, pwhash)


# ----------------------------------------------------------------- пользователи

def create_user(conn: sqlite3.Connection, login: str, name: str, role: str,
                password: str, tg_user_id: int | None = None) -> int:
    if role not in ROLES:
        raise ValueError(f"неизвестная роль: {role}")
    salt, pwhash = hash_password(password)
    cur = conn.execute(
        "INSERT INTO users (login, name, role, tg_user_id, salt, pwhash) "
        "VALUES (?,?,?,?,?,?)",
        (login.strip().lower(), name.strip(), role, tg_user_id, salt, pwhash))
    return int(cur.lastrowid)


def set_password(conn: sqlite3.Connection, user_id: int, password: str) -> None:
    salt, pwhash = hash_password(password)
    conn.execute("UPDATE users SET salt=?, pwhash=? WHERE id=?",
                 (salt, pwhash, user_id))


def user_by_login(conn: sqlite3.Connection, login: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE login=? AND active=1",
                        (login.strip().lower(),)).fetchone()


def user_by_id(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def all_users(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM users ORDER BY role, name").fetchall()


# ----------------------------------------------------------------- сессии

def open_session(conn: sqlite3.Connection, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    conn.execute("INSERT INTO sessions (token, user_id, expires_at) VALUES (?,?,?)",
                 (token, user_id, expires.isoformat(timespec="seconds")))
    return token


def session_user(conn: sqlite3.Connection, token: str | None) -> sqlite3.Row | None:
    if not token:
        return None
    row = conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token=? AND u.active=1 AND s.expires_at > ?",
        (token, datetime.now(timezone.utc).isoformat(timespec="seconds"))).fetchone()
    return row


def close_session(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))


def drop_expired_sessions(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM sessions WHERE expires_at <= ?",
                 (datetime.now(timezone.utc).isoformat(timespec="seconds"),))


# ----------------------------------------------------------------- привязки

def mentor_participants(conn: sqlite3.Connection, mentor_id: int) -> list[int]:
    return [r["tg_user_id"] for r in conn.execute(
        "SELECT tg_user_id FROM assignments WHERE mentor_id=?", (mentor_id,))]


def set_assignments(conn: sqlite3.Connection, mentor_id: int,
                    tg_ids: Iterable[int]) -> None:
    # Замена списка целиком: снятие и назначение должны быть одной операцией,
    # иначе в середине ментор увидит пустой список.
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM assignments WHERE mentor_id=?", (mentor_id,))
        conn.executemany("INSERT OR IGNORE INTO assignments VALUES (?,?)",
                         [(mentor_id, int(t)) for t in tg_ids])
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def assignments_by_participant(conn: sqlite3.Connection) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for r in conn.execute(
            "SELECT a.tg_user_id AS tg, u.name FROM assignments a "
            "JOIN users u ON u.id = a.mentor_id"):
        out.setdefault(r["tg"], []).append(r["name"])
    return out


# ----------------------------------------------------------------- журнал

def log(conn: sqlite3.Connection, user: sqlite3.Row | None, action: str,
        target: str | None = None, detail: str | None = None) -> None:
    conn.execute(
        "INSERT INTO access_log (user_id, login, role, action, target, detail) "
        "VALUES (?,?,?,?,?,?)",
        (user["id"] if user else None, user["login"] if user else None,
         user["role"] if user else None, action, target, detail))


def recent_log(conn: sqlite3.Connection, limit: int = 200) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM access_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def log_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(
        timespec="seconds")
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM access_log "
        "WHERE action='открыл профиль' AND at > ?", (week,)).fetchone()
    return {"profiles_week": row["n"] if row else 0}


# ----------------------------------------------------------------- настройки

def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO settings (key, value) VALUES (?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
