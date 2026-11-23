"""Чтение базы знаний: участники, поведенческие метрики, высказывания, поиск.

Метрики считаются формулами сборщика (kb.build) — так цифры в приложении
совпадают с уже собранными досье, а не живут своей жизнью.

База знаний открывается только на чтение. Всё, что приложение пишет,
идёт в app.sqlite: там же лежит поисковый индекс.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from . import store
from .config import KB_DB, KB_SRC

MSK = timezone(timedelta(hours=3))

if str(KB_SRC) not in sys.path:
    sys.path.insert(0, str(KB_SRC))

from kb import build as kb_build  # noqa: E402

# Порог тишины: за сколько дней без сообщений участник считается притихшим
# и выпавшим. Отсчёт — от последнего сообщения в чате, а не от «сегодня»:
# корпус статичный, выгрузка сделана 3 сентября.
QUIET_DAYS = 7
LOST_DAYS = 21

# Корпус — чаты закончившихся потоков (ММ5, ММ6). После финала «притих»
# и «выпал» ничего не значат: человек не молчит, поток просто кончился.
# Поэтому состояние у всех одно — «поток завершён». Если в корпус снова
# попадёт живой чат, выключить: VERSHINA_CORPUS_FINISHED=0.
FINISHED = os.environ.get("VERSHINA_CORPUS_FINISHED", "1") != "0"
FINISHED_STATUS = "поток завершён"

MIN_MESSAGES = 5  # столько же, сколько у сборщика: ниже порога досье не строится


class KbMissing(RuntimeError):
    pass


def kb_conn() -> sqlite3.Connection:
    if not KB_DB.exists():
        raise KbMissing(
            f"База знаний не найдена: {KB_DB}. Соберите её через vershina-kb "
            f"или укажите путь в VERSHINA_KB_DB.")
    conn = sqlite3.connect(f"file:{KB_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ------------------------------------------------------------------ кэш

_cache: dict[str, Any] = {"stamp": None}


def _stamp() -> tuple[float, int]:
    st = KB_DB.stat()
    return (st.st_mtime, st.st_size)


def _data() -> dict[str, Any]:
    """Весь корпус в памяти. 28 тысяч сообщений — это десятки мегабайт,
    зато карточка участника открывается без пересчёта по диску.
    Кэш сбрасывается, как только база знаний изменилась."""
    stamp = _stamp()
    if _cache.get("stamp") == stamp:
        return _cache["data"]

    with kb_conn() as conn:
        chats = [dict(r) for r in conn.execute(
            "SELECT id, title, username, kind FROM chats ORDER BY title")]
        users = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM users")}
        rows_by_chat = {c["id"]: kb_build.fetch_rows(conn, c["id"]) for c in chats}

    by_user: dict[int, list[dict]] = defaultdict(list)
    chat_last: dict[int, datetime] = {}
    for cid, rows in rows_by_chat.items():
        for r in rows:
            r["chat_id"] = cid
            by_user[r["user_id"]].append(r)
        if rows:
            chat_last[cid] = kb_build._dt(rows[-1]["date"])

    for uid in by_user:
        by_user[uid].sort(key=lambda r: r["date"])

    corpus_last = max(chat_last.values()) if chat_last else datetime.now(timezone.utc)

    data = {
        "chats": chats,
        "chat_titles": {c["id"]: c["title"] for c in chats},
        "users": users,
        "rows_by_chat": rows_by_chat,
        "by_user": dict(by_user),
        "chat_last": chat_last,
        "corpus_last": corpus_last,
        "profiles": {},
    }
    _cache["stamp"] = stamp
    _cache["data"] = data
    return data


def reset_cache() -> None:
    _cache["stamp"] = None


# ------------------------------------------------------------------ участники

def name_of(uid: int) -> str:
    if uid < 0:
        # участник нового потока: пока известен только по таблице декларации
        from . import store
        conn = store.connect()
        try:
            row = conn.execute("SELECT name FROM people WHERE id=?", (-uid,)).fetchone()
        finally:
            conn.close()
        return row["name"] if row else f"участник {-uid}"
    d = _data()
    u = d["users"].get(uid)
    if not u:
        return f"id{uid}"
    parts = [u.get("first_name"), u.get("last_name")]
    name = " ".join(p for p in parts if p).strip()
    return name or (f"@{u['username']}" if u.get("username") else f"id{uid}")


def _status(last_seen: datetime, ref: datetime) -> tuple[str, int]:
    silent = max(0, (ref - last_seen).days)
    if FINISHED:
        return FINISHED_STATUS, 0
    if silent <= QUIET_DAYS:
        return "активен", silent
    if silent <= LOST_DAYS:
        return "притих", silent
    return "выпал", silent


def profile_of(uid: int) -> dict[str, Any] | None:
    """Поведенческий профиль участника: метрики сборщика плюс то,
    что нужно приложению — статус тишины, разбивка по чатам, динамика."""
    d = _data()
    rows = d["by_user"].get(uid)
    if not rows:
        return None
    if uid in d["profiles"]:
        return d["profiles"][uid]

    prof = kb_build.profile(rows)
    dates = [kb_build._dt(r["date"]) for r in rows]
    ref = d["corpus_last"]
    status, silent = _status(dates[-1], ref)

    chats = Counter(d["chat_titles"].get(r["chat_id"], "—") for r in rows)
    weeks = Counter(kb_build._dt(r["date"]).astimezone(MSK).strftime("%G-%V")
                    for r in rows)

    last14 = sum(1 for dt in dates if dt > ref - timedelta(days=14))
    prev14 = sum(1 for dt in dates
                 if ref - timedelta(days=28) < dt <= ref - timedelta(days=14))

    u = d["users"].get(uid, {})
    prof.update({
        "user_id": uid,
        "name": name_of(uid),
        "username": u.get("username"),
        "is_bot": bool(u.get("is_bot")),
        "status": status,
        "silent_days": silent,
        "chats": dict(chats.most_common()),
        "by_week": dict(sorted(weeks.items())),
        "last14": last14,
        "prev14": prev14,
        "trend": last14 - prev14,
        "audio_hours": round((prof.get("audio_min") or 0) / 60, 1),
        "peak_hour_label": (f"{prof['peak_hour']:02d}:00"
                            if prof.get("peak_hour") is not None else None),
    })
    d["profiles"][uid] = prof
    return prof


def participants(chat_id: int | None = None, only: list[int] | None = None,
                 min_messages: int = MIN_MESSAGES) -> list[dict[str, Any]]:
    d = _data()
    out = []
    for uid, rows in d["by_user"].items():
        if uid is None:
            continue
        if only is not None and uid not in only:
            continue
        if chat_id and not any(r["chat_id"] == chat_id for r in rows):
            continue
        if len(rows) < min_messages:
            continue
        prof = profile_of(uid)
        if not prof or prof["is_bot"]:
            continue
        out.append(prof)
    out.sort(key=lambda p: -p["messages"])
    return out


def messages_of(uid: int, chat_id: int | None = None, offset: int = 0,
                limit: int = 50, spoken_only: bool = False) -> tuple[list[dict], int]:
    d = _data()
    rows = [r for r in d["by_user"].get(uid, []) if r["content"]]
    if chat_id:
        rows = [r for r in rows if r["chat_id"] == chat_id]
    if spoken_only:
        rows = [r for r in rows if r["is_spoken"]]
    total = len(rows)
    page = rows[offset:offset + limit]
    return [_view(r) for r in page], total


def _view(r: dict[str, Any]) -> dict[str, Any]:
    d = _data()
    dt = kb_build._dt(r["date"]).astimezone(MSK)
    return {
        "chat_id": r["chat_id"],
        "chat": d["chat_titles"].get(r["chat_id"], "—"),
        "msg_id": r["msg_id"],
        "user_id": r["user_id"],
        "author": name_of(r["user_id"]) if r["user_id"] else "—",
        "when": dt.strftime("%d.%m.%Y %H:%M"),
        "date": dt,
        "kind": ("кружок" if r["media_kind"] == "video_note" else
                 "голосовое" if r["media_kind"] == "voice" else "текст"),
        "spoken": r["is_spoken"],
        "duration": r.get("duration"),
        "reply_to": r.get("reply_to"),
        "text": r["content"],
    }


def message(chat_id: int, msg_id: int) -> dict[str, Any] | None:
    d = _data()
    for r in d["rows_by_chat"].get(chat_id, []):
        if r["msg_id"] == msg_id:
            return _view(r)
    return None


def thread_around(chat_id: int, msg_id: int, span: int = 3) -> list[dict[str, Any]]:
    """Соседние сообщения — без контекста реплика из чата читается неверно."""
    rows = _data()["rows_by_chat"].get(chat_id, [])
    idx = next((i for i, r in enumerate(rows) if r["msg_id"] == msg_id), None)
    if idx is None:
        return []
    lo, hi = max(0, idx - span), min(len(rows), idx + span + 1)
    return [_view(r) for r in rows[lo:hi] if r["content"]]


# ------------------------------------------------------------------ сводки

def overview() -> dict[str, Any]:
    d = _data()
    people = participants()
    ref = d["corpus_last"]
    total_msgs = sum(len(r) for r in d["rows_by_chat"].values())
    spoken = [r for rows in d["rows_by_chat"].values() for r in rows if r["is_spoken"]]
    return {
        "chats": d["chats"],
        "messages": total_msgs,
        "participants": len(people),
        "spoken": len(spoken),
        "audio_hours": round(sum(r["audio_secs"] or 0 for r in spoken) / 3600, 1),
        "transcribed": sum(1 for r in spoken if r["transcript"]),
        "first": min((kb_build._dt(rows[0]["date"]) for rows in
                      d["rows_by_chat"].values() if rows), default=None),
        "last": ref,
        "active": sum(1 for p in people if p["status"] == "активен"),
        "quiet": sum(1 for p in people if p["status"] == "притих"),
        "lost": sum(1 for p in people if p["status"] == "выпал"),
    }


def chat_activity(chat_id: int) -> dict[str, int]:
    rows = _data()["rows_by_chat"].get(chat_id, [])
    weeks = Counter(kb_build._dt(r["date"]).astimezone(MSK).strftime("%G-%V")
                    for r in rows)
    return dict(sorted(weeks.items()))


# ------------------------------------------------------------------ поиск

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def reindex(conn: sqlite3.Connection | None = None) -> int:
    """Перестроить поисковый индекс из базы знаний."""
    own = conn is None
    conn = conn or store.connect()
    d = _data()
    conn.execute("DELETE FROM corpus_fts")
    batch = []
    for cid, rows in d["rows_by_chat"].items():
        for r in rows:
            if not r["content"]:
                continue
            batch.append((r["content"], cid, r["msg_id"], r["user_id"], r["date"],
                          "кружок" if r["media_kind"] == "video_note" else
                          "голосовое" if r["media_kind"] == "voice" else "текст"))
    conn.executemany(
        "INSERT INTO corpus_fts (text, chat_id, msg_id, user_id, date, kind) "
        "VALUES (?,?,?,?,?,?)", batch)
    conn.commit()
    if own:
        conn.close()
    return len(batch)


def _fts_query(raw: str) -> str:
    """Пользовательский запрос → выражение FTS5. Кавычки и операторы
    экранируются: человек пишет обычными словами, а не синтаксисом."""
    words = _TOKEN.findall(raw)
    if not words:
        return ""
    return " AND ".join(f'"{w}"*' for w in words)


def search(conn: sqlite3.Connection, query: str, user_ids: list[int] | None = None,
           chat_id: int | None = None, spoken: str = "all",
           limit: int = 60) -> list[dict[str, Any]]:
    expr = _fts_query(query)
    if not expr:
        return []
    sql = ["SELECT chat_id, msg_id, user_id, date, kind, "
           "snippet(corpus_fts, 0, '<mark>', '</mark>', '…', 24) AS excerpt, "
           "bm25(corpus_fts) AS rank FROM corpus_fts WHERE corpus_fts MATCH ?"]
    args: list[Any] = [expr]
    if user_ids is not None:
        if not user_ids:
            return []
        sql.append(f"AND user_id IN ({','.join('?' * len(user_ids))})")
        args += list(user_ids)
    if chat_id:
        sql.append("AND chat_id = ?")
        args.append(chat_id)
    if spoken == "voice":
        sql.append("AND kind IN ('кружок','голосовое')")
    elif spoken == "text":
        sql.append("AND kind = 'текст'")
    sql.append("ORDER BY rank LIMIT ?")
    args.append(limit)

    d = _data()
    out = []
    for r in conn.execute(" ".join(sql), args):
        dt = kb_build._dt(r["date"]).astimezone(MSK)
        out.append({
            "chat_id": r["chat_id"], "msg_id": r["msg_id"],
            "user_id": r["user_id"],
            "author": name_of(r["user_id"]) if r["user_id"] else "—",
            "chat": d["chat_titles"].get(r["chat_id"], "—"),
            "when": dt.strftime("%d.%m.%Y %H:%M"),
            "kind": r["kind"], "excerpt": r["excerpt"],
        })
    return out
