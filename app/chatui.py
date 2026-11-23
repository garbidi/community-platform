"""«Посты» как телеграм внутри платформы: чаты потока, переписка, черновики.

Только для организаторов. Платформа читает чаты аккаунтом сообщества и
показывает их как в телеграме; писать можно черновиком, отправка — кнопкой
и только при открытом замке (tg_account.sending_allowed). Ничего не отмечается
прочитанным: у Марьи в телефоне непрочитанные остаются как были.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import emoji, flows, reports, tg_account

MEDIA = Path("/data/tgmedia")
AVATARS = Path("/data/tgavatars")
MSK = timezone(timedelta(hours=3))

GROUPS = (("поток", "Участники"), ("команда", "Команды"), ("отчёты", "Отчёты"),
          ("организаторы", "Организаторы"), ("чеки", "Чеки"))


def chat_list(conn: sqlite3.Connection, flow_id: int) -> list[dict]:
    """Чаты потока по ролям, с последним сообщением — как список чатов в телеграме."""
    rows = flows.chats(conn, flow_id)
    out = []
    for kind, title in GROUPS:
        items = []
        for c in rows:
            if c["kind"] != kind:
                continue
            q = "SELECT * FROM chat_messages WHERE chat_id=?" + (" AND topic=?" if c["topic_id"] else "") + \
                " ORDER BY at DESC LIMIT 1"
            last = conn.execute(q, (c["chat_id"], c["topic_id"]) if c["topic_id"] else (c["chat_id"],)).fetchone()
            items.append({"chat_id": c["chat_id"], "topic_id": c["topic_id"], "title": flows.chat_label(c),
                          "team": c["team_title"], "last": last,
                          "last_at": _local(last["at"]) if last else None,
                          "last_text": ((last["text"] or ("📷 " + last["media"] if last["media"] else ""))[:70]
                                        if last else ""),
                          "last_who": last["sender"] if last else ""})
        items.sort(key=lambda x: x["last"]["at"] if x["last"] else "", reverse=True)
        if items:
            out.append({"kind": kind, "title": title, "chats": items})
    return out


def _local(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(MSK)


def messages(conn: sqlite3.Connection, chat_id: int, topic: int = 0, before: str | None = None,
             limit: int = 60) -> list[dict]:
    """Переписка чата (новые внизу), с ответами, альбомами и эмодзи."""
    q = "SELECT * FROM chat_messages WHERE chat_id=?"
    args: list[Any] = [chat_id]
    if topic:
        q += " AND (topic=? OR msg_id=?)"
        args += [topic, topic]
    if before:
        q += " AND at<?"
        args.append(before)
    rows = list(reversed(conn.execute(q + " ORDER BY at DESC LIMIT ?", (*args, limit)).fetchall()))
    by_id = {r["msg_id"]: r for r in rows}
    out, prev = [], None
    for r in rows:
        rep = None
        if r["reply_to"] and r["reply_to"] != topic:
            rr = by_id.get(r["reply_to"]) or conn.execute(
                "SELECT sender, text, media FROM chat_messages WHERE chat_id=? AND msg_id=?",
                (chat_id, r["reply_to"])).fetchone()
            if rr:
                rep = {"who": rr["sender"], "text": (rr["text"] or rr["media"] or "")[:90]}
        at = _local(r["at"])
        out.append({
            "id": r["msg_id"], "at": at, "day": at.date(), "who": r["sender"] or "—", "sender_id": r["sender_id"],
            "out": bool(r["outgoing"]), "html": emoji.message_html(r["text"], json.loads(r["emojis"]) if r["emojis"] else None),
            "media": r["media"], "img": (MEDIA / f"{chat_id}_{r['msg_id']}.jpg").exists(),
            "album": r["album"], "reply": rep,
            # подряд от одного человека — имя и аватарку показываем один раз
            "cont": bool(prev and prev["sender_id"] == r["sender_id"] and (at - prev["at"]).seconds < 600
                         and prev["day"] == at.date()),
            "avatar": (AVATARS / f"{r['sender_id']}.jpg").exists(),
            "initials": "".join(w[0] for w in (r["sender"] or "?").split()[:2]).upper(),
        })
        prev = out[-1]
    return out


def refresh(conn: sqlite3.Connection, chat_id: int, force: bool = False) -> dict:
    """Дочитать свежие сообщения чата (не чаще раза в минуту) и подтянуть
    превью картинок и аватарки для последних сообщений. Только чтение."""
    last = conn.execute("SELECT at FROM chat_reads WHERE chat_id=?", (chat_id,)).fetchone()
    fresh = last and datetime.fromisoformat(last["at"]) > datetime.now() - timedelta(minutes=1)
    res = {"ok": True}
    if force or not fresh:
        res = reports.fetch(conn, chat_id, days=3)
    _pictures(conn, chat_id, conn.execute("SELECT msg_id, media, sender_id FROM chat_messages WHERE chat_id=? "
                                          "ORDER BY at DESC LIMIT 80", (chat_id,)).fetchall())
    return res


def older(conn: sqlite3.Connection, chat_id: int, topic: int, before_id: int, limit: int = 60) -> list[dict]:
    """Сообщения старше before_id — при прокрутке переписки вверх. Чего нет
    в базе, дочитываем из телеграма (только чтение)."""
    row = conn.execute("SELECT at FROM chat_messages WHERE chat_id=? AND msg_id=?", (chat_id, before_id)).fetchone()
    if not row:
        return []
    q = "SELECT msg_id, media, sender_id FROM chat_messages WHERE chat_id=? AND at<?"
    args: list[Any] = [chat_id, row["at"]]
    if topic:
        q += " AND (topic=? OR msg_id=?)"
        args += [topic, topic]
    # в базе могут быть пропуски — поэтому читаем ровно то, что идёт перед before_id
    reports.fetch(conn, chat_id, before_id=before_id, limit=300 if topic else 100)
    have = conn.execute(q + " ORDER BY at DESC LIMIT ?", (*args, limit)).fetchall()
    _pictures(conn, chat_id, have)
    return messages(conn, chat_id, topic, before=row["at"], limit=limit)


def _pictures(conn: sqlite3.Connection, chat_id: int, rows) -> None:
    """Превью картинок и аватарки авторов для этих сообщений."""
    MEDIA.mkdir(parents=True, exist_ok=True)
    AVATARS.mkdir(parents=True, exist_ok=True)
    need = [r["msg_id"] for r in rows if r["media"] in ("фото", "видео", "кружок", "файл")
            and not (MEDIA / f"{chat_id}_{r['msg_id']}.jpg").exists()]
    if need:
        tg_account.fetch_media(chat_id, need[:40], MEDIA)
    people = sorted({r["sender_id"] for r in rows if r["sender_id"]
                     and not (AVATARS / f"{r['sender_id']}.jpg").exists()
                     and not (AVATARS / f"{r['sender_id']}.jpg.none").exists()})
    if people:
        tg_account.fetch_avatars(people[:40], AVATARS)


def facts(conn: sqlite3.Connection, flow: sqlite3.Row) -> dict:
    """Что пригодится при написании поста: вовлечения, отчёты за вчера."""
    from . import board
    b = board.board(conn, flow)
    rep = None
    if reports.report_chats(conn, flow["id"]):
        day = (datetime.now(MSK) - timedelta(days=1)).date()
        r = reports.day_report(conn, flow, day)
        rep = {"day": day, "done": r["done"], "total": r["total"], "missing": [m["name"] for m in r["missing"]]}
    return {"board": b, "reports": rep}
