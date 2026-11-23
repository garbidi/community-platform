"""Отчёты участников за день — из чата с ролью «Отчёты участников».

Участники каждый вечер пишут в чат отчёт («Отчет 12.10, #ФамилияИмя, что
сделал, вовлечение, состояние»). Платформа читает чат аккаунтом сообщества —
только читает, ничего туда не пишет, — и по каждому участнику потока решает,
сдан ли отчёт за день. Утром кладёт в «Посты» черновик «отчёты за вчера»;
отправляет его организатор кнопкой.

Правила дня (ответ Марьи 15.10.2026):
- отчёт засчитывается за день отправки по Москве, срок — 23:59 этого дня;
  позже — это уже следующий день, а за пропущенный — штраф;
- дата в тексте («Отчет 11.10») день не меняет, а даёт пометку; отчёт со
  вчерашней датой, присланный после полуночи, помечается «штраф за 11.10»;
- считается только текст: кружок, голосовое, файл без текста — не отчёт
  (у Близниченко кружок — дополнительное условие, а не замена);
- воскресенье — выходной, отчёт не нужен; личные выходные — таблица
  report_days_off (Близниченко может не сдавать по субботам);
- ответы на чужие сообщения («Умница, Таня!») — не отчёт.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import calls, flows, store, tg_account

MSK = timezone(timedelta(hours=3))
DAY_STARTS = 0          # день кончается в 23:59 — после полуночи уже следующий
DAYS_OFF = {6}          # воскресенье — выходной для всех
MORNING = 9             # с 9 утра — готовить черновик «отчёты за вчера»

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_messages (
    chat_id   INTEGER NOT NULL,
    msg_id    INTEGER NOT NULL,
    at        TEXT NOT NULL,          -- UTC, ISO
    sender_id INTEGER,
    username  TEXT,
    sender    TEXT,                   -- имя в телеграме
    text      TEXT,
    media     TEXT,                   -- кружок | голос | фото | видео | файл
    reply     INTEGER NOT NULL DEFAULT 0,
    emojis    TEXT,                   -- JSON [[смещение, длина, id картинки]] премиум-эмодзи
    PRIMARY KEY (chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS chat_messages_at ON chat_messages (chat_id, at);
CREATE TABLE IF NOT EXISTS chat_senders (
    sender_id INTEGER PRIMARY KEY,    -- кто в телеграме
    card      INTEGER,                -- карточка участника; NULL — не участник
    how       TEXT,                   -- вручную | по нику | по имени | по хэштегу
    set_by    TEXT, set_at TEXT
);
CREATE TABLE IF NOT EXISTS chat_reads (
    chat_id INTEGER PRIMARY KEY, at TEXT, ok INTEGER, note TEXT
);
-- личные выходные по отчётам: участник может не сдавать в этот день недели
CREATE TABLE IF NOT EXISTS report_days_off (
    flow    TEXT NOT NULL,
    name    TEXT NOT NULL,                -- как в people.name: «Фамилия Имя»
    weekday INTEGER NOT NULL,             -- 0 — пн … 6 — вс
    note    TEXT,
    PRIMARY KEY (flow, name, weekday)
);
"""


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(chat_messages)")}
    for col, typ in (("emojis", "TEXT"), ("reply_to", "INTEGER"), ("topic", "INTEGER"),
                     ("album", "INTEGER"), ("outgoing", "INTEGER")):
        if col not in cols:
            conn.execute(f"ALTER TABLE chat_messages ADD COLUMN {col} {typ}")
    # со слов Марьи 15.10.2026
    conn.execute("INSERT OR IGNORE INTO report_days_off VALUES ('ММ7', 'Близниченко Надежда', 5, "
                 "'может не сдавать отчёт по субботам (Марья, 15.10)')")


def is_day_off(day: date) -> bool:
    return day.weekday() in DAYS_OFF


def days_off(conn: sqlite3.Connection, flow: str) -> dict[str, set[int]]:
    """Личные выходные: имя участника → дни недели."""
    out: dict[str, set[int]] = {}
    for r in conn.execute("SELECT name, weekday FROM report_days_off WHERE flow=?", (flow,)):
        out.setdefault(r["name"], set()).add(r["weekday"])
    return out


def report_chats(conn: sqlite3.Connection, flow_id: int) -> list[sqlite3.Row]:
    return [c for c in flows.chats(conn, flow_id) if c["kind"] == "отчёты"]


# ----------------------------------------------------------------- чтение чата

def fetch(conn: sqlite3.Connection, chat_id: int, days: int = 3, before_id: int = 0, limit: int = 100) -> dict:
    """Дочитать чат за последние дни в базу (повторы не дублируются).
    before_id — вместо этого limit сообщений старше него (история)."""
    if before_id:
        res = tg_account.read_messages(chat_id, None, limit=limit, before_id=before_id)
    else:
        res = tg_account.read_messages(chat_id, datetime.now(timezone.utc) - timedelta(days=days))
    now = datetime.now().isoformat(timespec="seconds")
    if before_id and res.get("ok"):
        _store(conn, chat_id, res["messages"])
        return {"ok": True, "n": len(res["messages"])}
    if not res.get("ok"):
        conn.execute("INSERT OR REPLACE INTO chat_reads VALUES (?,?,0,?)", (chat_id, now, res.get("error")))
        return res
    msgs = res["messages"]
    _store(conn, chat_id, msgs, (chat_id, now, f"прочитано сообщений: {len(msgs)}"))
    return {"ok": True, "n": len(msgs)}


def _store(conn: sqlite3.Connection, chat_id: int, msgs: list[dict], read: tuple | None = None) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        for m in msgs:
            conn.execute("INSERT OR REPLACE INTO chat_messages "
                         "(chat_id, msg_id, at, sender_id, username, sender, text, media, reply, emojis, "
                         "reply_to, topic, album, outgoing) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (chat_id, m["id"], m["date"], m["sender_id"], m["username"], m["name"],
                          m["text"], m["media"], 1 if m["reply"] else 0,
                          json.dumps(m["emojis"]) if m.get("emojis") else None,
                          m.get("reply_to"), m.get("topic"), m.get("album"), 1 if m.get("out") else 0))
        if read:
            conn.execute("INSERT OR REPLACE INTO chat_reads VALUES (?,?,1,?)", read)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ----------------------------------------------------------------- кто есть кто

_HASHTAG = re.compile(r"#([\wА-Яа-яЁё_]+(?:\s+[А-ЯЁ][а-яё]+)?)")


def _hashtag_name(text: str) -> str | None:
    """«#СветланаКоролёва», «#Роман Сидоренко», «#Белов Илья» → слова имени."""
    m = _HASHTAG.search(text or "")
    if not m:
        return None
    raw = m.group(1).replace("_", " ")
    raw = re.sub(r"(?<=[а-яёa-z])(?=[А-ЯЁA-Z])", " ", raw)     # «СветланаКоролёва» → два слова
    return raw.strip() or None


def _people(conn: sqlite3.Connection, flow: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT p.*, t.title AS team_title FROM people p LEFT JOIN teams t ON t.id=p.team_id "
        "WHERE p.flow=? AND p.status='в потоке' ORDER BY t.title, p.name", (flow,)).fetchall()


def resolve_senders(conn: sqlite3.Connection, flow: str, chat_id: int) -> dict[int, dict]:
    """Отправитель → участник: ручная отметка → ник/телеграм из базы →
    имя в телеграме → хэштег в отчёте. Узнанное запоминается."""
    from .roster import card_id
    people = _people(conn, flow)
    cards = {card_id(p["id"]): p for p in people}
    by_tg = {p["tg_user_id"]: card_id(p["id"]) for p in people if p["tg_user_id"]}
    by_nick = {(p["username"] or "").lower(): card_id(p["id"]) for p in people if p["username"]}
    index = [c for c in calls.roster_index(conn) if c["kind"] == "участник" and c["ref"] in cards]
    known = {r["sender_id"]: r for r in conn.execute("SELECT * FROM chat_senders")}
    out: dict[int, dict] = {}
    senders = conn.execute(
        "SELECT sender_id, MAX(username) username, MAX(sender) sender, "
        "GROUP_CONCAT(CASE WHEN reply=0 THEN text END, char(10)) texts "
        "FROM chat_messages WHERE chat_id=? AND sender_id IS NOT NULL GROUP BY sender_id", (chat_id,)).fetchall()
    for s in senders:
        sid = s["sender_id"]
        k = known.get(sid)
        if k and k["how"] == "вручную":
            out[sid] = {"card": k["card"], "how": "вручную"}
            continue
        card, how = by_tg.get(sid), "по телеграму"
        if not card and s["username"]:
            card, how = by_nick.get(s["username"].lower()), "по нику"
        if not card and s["sender"]:
            g = calls._guess(s["sender"], index)
            if g and g["sure"]:
                card, how = g["ref"], "по имени"
        if not card:
            tag = _hashtag_name(s["texts"] or "")
            g = calls._guess(tag, index) if tag else None
            if g:
                card, how = g["ref"], "по хэштегу"
        if not card and s["sender"]:
            g = calls._guess(s["sender"], index)       # одно имя/фамилия — догадка
            if g:
                card, how = g["ref"], "догадка"
        out[sid] = {"card": card, "how": how if card else None}
        if card and how != "догадка" and not k:
            conn.execute("INSERT OR IGNORE INTO chat_senders VALUES (?,?,?,?,?)",
                         (sid, card, how, "авто", datetime.now().isoformat(timespec="seconds")))
    return out


def set_sender(conn: sqlite3.Connection, sender_id: int, card: int | None, who: str) -> None:
    conn.execute("INSERT OR REPLACE INTO chat_senders VALUES (?,?,?,?,?)",
                 (sender_id, card, "вручную", who, datetime.now().isoformat(timespec="seconds")))


# ----------------------------------------------------------------- день

_LABEL = re.compile(r"отч[её]т\D{0,12}(\d{1,2})[.\-/](\d{1,2})", re.I)


def day_of(at_utc: str) -> date:
    """День, за который засчитан отчёт: день отправки по Москве."""
    t = datetime.fromisoformat(at_utc).astimezone(MSK)
    return (t - timedelta(hours=DAY_STARTS)).date()


# признаки отчёта в тексте: слово «отчёт», хэштег с именем, строка шаблона
_REPORTISH = re.compile(r"отч[её]т|#\w|что\s+я\s+(сегодня\s+)?сделал|для\s+(движения|достижения)\s+к?\s*цел", re.I)


def is_report(text: str, media: str | None, reply: int) -> bool:
    if reply:
        return False
    t = (text or "").strip()
    if len(t) < 40:
        return False                     # кружок, голосовое, файл без текста — не отчёт (Марья)
    return bool(_REPORTISH.search(t)) or len(t) >= 200


def day_report(conn: sqlite3.Connection, flow: sqlite3.Row, day: date,
               ids: list[int] | None = None) -> dict[str, Any]:
    """Кто сдал отчёт за день, кто нет, что необычно. ids — видимые роли карточки."""
    from .roster import card_id
    chats = report_chats(conn, flow["id"])
    people = [p for p in _people(conn, flow["name"]) if ids is None or card_id(p["id"]) in ids]
    off = days_off(conn, flow["name"])
    cache: dict[date, set[int]] = {}
    got: dict[int, list[dict]] = {}
    strangers: dict[int, dict] = {}
    lo = datetime.combine(day, datetime.min.time(), MSK) + timedelta(hours=DAY_STARTS)
    hi = lo + timedelta(days=1)
    for ch in chats:
        who = resolve_senders(conn, flow["name"], ch["chat_id"])
        for m in conn.execute(
                "SELECT * FROM chat_messages WHERE chat_id=? AND at>=? AND at<? ORDER BY at",
                (ch["chat_id"], lo.astimezone(timezone.utc).isoformat(), hi.astimezone(timezone.utc).isoformat())):
            if not is_report(m["text"], m["media"], m["reply"]):
                continue
            card = (who.get(m["sender_id"]) or {}).get("card")
            lab = _LABEL.search(m["text"] or "")
            label = None
            if lab:
                try:
                    label = date(day.year, int(lab.group(2)), int(lab.group(1)))
                except ValueError:
                    label = None
            item = {"at": datetime.fromisoformat(m["at"]).astimezone(MSK), "media": m["media"],
                    "label": label, "chat_id": ch["chat_id"], "msg_id": m["msg_id"],
                    "templated": bool(_REPORTISH.search(m["text"] or "")),
                    "link": _link(ch["chat_id"], m["msg_id"]),
                    "how": (who.get(m["sender_id"]) or {}).get("how")}
            if card:
                got.setdefault(card, []).append(item)
            else:
                strangers.setdefault(m["sender_id"], {"sender": m["sender"], "username": m["username"],
                                                      "sender_id": m["sender_id"], "msgs": []})["msgs"].append(item)
    rows, missing, odd = [], [], []
    for p in people:
        card = card_id(p["id"])
        items = got.get(card, [])
        team = (p["team_title"] or "").split("—")[0].strip()
        name = " ".join(reversed(p["name"].split())) if len(p["name"].split()) == 2 else p["name"]
        rest = is_day_off(day) or day.weekday() in off.get(p["name"], set())
        row = {"card": card, "name": name, "team": team, "msgs": items, "ok": bool(items), "off": rest}
        notes = []
        if items:
            labels = {i["label"] for i in items if i["label"]}
            # штраф — только если за подписанный день отчёта нет и день был рабочим
            late = sorted(d for d in labels if d < day and not is_day_off(d)
                          and d.weekday() not in off.get(p["name"], set())
                          and card not in _cards_on(conn, flow, d, cache))
            if late and day not in labels:
                # «Отчёт 14.10», присланный 15.10 после полуночи: за 14.10 — штраф
                notes.append("подписан " + ", ".join(d.strftime("%d.%m") for d in late)
                             + " — за тот день отчёта не было: штраф")
            elif labels and day not in labels:
                notes.append("подписан " + ", ".join(sorted(d.strftime("%d.%m") for d in labels)))
            if not any(i["templated"] for i in items):
                notes.append("не по шаблону")
            if any(i["how"] == "догадка" for i in items):
                notes.append("отправитель узнан по одному имени — проверьте")
        elif not rest:
            missing.append(row)
        row["notes"] = notes
        if notes:
            odd.append(row)
        rows.append(row)
    # в выходной считаем только тех, кто всё-таки сдал; должников нет
    due = [r for r in rows if not r["off"] or r["ok"]]
    return {"day": day, "rows": rows, "missing": missing, "odd": odd, "day_off": is_day_off(day),
            "done": sum(1 for r in due if r["ok"]), "total": len(due),
            "strangers": list(strangers.values()) if ids is None else [], "chats": chats}


def _cards_on(conn: sqlite3.Connection, flow: sqlite3.Row, day: date, cache: dict) -> set[int]:
    """Кто сдал отчёт в этот день (по времени отправки) — для пометки «штраф»."""
    if day not in cache:
        lo = datetime.combine(day, datetime.min.time(), MSK) + timedelta(hours=DAY_STARTS)
        hi = lo + timedelta(days=1)
        cards = set()
        for ch in report_chats(conn, flow["id"]):
            who = resolve_senders(conn, flow["name"], ch["chat_id"])
            for m in conn.execute("SELECT * FROM chat_messages WHERE chat_id=? AND at>=? AND at<?",
                                  (ch["chat_id"], lo.astimezone(timezone.utc).isoformat(),
                                   hi.astimezone(timezone.utc).isoformat())):
                c = (who.get(m["sender_id"]) or {}).get("card")
                if c and is_report(m["text"], m["media"], m["reply"]):
                    cards.add(c)
        cache[day] = cards
    return cache[day]


def _link(chat_id: int, msg_id: int) -> str | None:
    s = str(chat_id)
    return f"https://t.me/c/{s[4:]}/{msg_id}" if s.startswith("-100") else None


def draft_text(rep: dict, flow_name: str) -> str:
    d = rep["day"]
    head = f"Отчёты {flow_name} за {d:%d.%m}: сдали {rep['done']} из {rep['total']}"
    if not rep["missing"]:
        return head + " 🔥\nВсе на месте — так держать!"
    names = ", ".join(r["name"] for r in rep["missing"])
    return f"{head}.\nНе сдали до 23:59 (штраф): {names}."


# ----------------------------------------------------------------- каждое утро

def morning(conn: sqlite3.Connection) -> list[str]:
    """Утром: дочитать чаты отчётов и положить в «Посты» черновик «отчёты за вчера»
    (один на поток и день). В чат ничего не отправляется."""
    from . import posts
    now = datetime.now(MSK)
    if now.hour < MORNING:
        return []
    day = (now - timedelta(days=1)).date()
    done = []
    for f in flows.listing(conn):
        if f["status"] != "идёт" or not report_chats(conn, f["id"]):
            continue
        if is_day_off(day):
            continue                     # за воскресенье сводку не пишем
        tag = f"отчёты {day.isoformat()}"
        if conn.execute("SELECT 1 FROM posts WHERE flow_id=? AND kind='отчёты' AND note LIKE ?",
                        (f["id"], f"%{tag}%")).fetchone():
            continue
        for ch in report_chats(conn, f["id"]):
            fetch(conn, ch["chat_id"])
        rep = day_report(conn, f, day)
        posts.create_text(conn, f["id"], "отчёты", draft_text(rep, f["name"]),
                          note=f"Черновик собран автоматически ({tag}). Проверьте и отправьте сами.")
        done.append(f["name"])
    return done


def start_background() -> None:
    """Раз в 15 минут: после 9 утра — черновик «отчёты за вчера», если его ещё нет."""
    import threading, time

    def loop():
        while True:
            conn = store.connect()
            try:
                morning(conn)
            except Exception as exc:          # noqa: BLE001 — фоновая задача не должна падать
                print("reports:", exc, flush=True)
            finally:
                conn.close()
            time.sleep(900)
    threading.Thread(target=loop, daemon=True, name="reports").start()


# ----------------------------------------------------------------- история по дням

def history(conn: sqlite3.Connection, flow: sqlite3.Row, days: int = 60) -> dict[str, Any]:
    """Отчёты по дням для «Обзора», «Участников» и карточки: кто в какой день
    сдал, сколько подряд сдаёт, сколько подряд пропускает. Сегодня не считаем —
    день ещё идёт. Без чата отчётов — пусто."""
    from .roster import card_id
    chats = report_chats(conn, flow["id"])
    if not chats:
        return {"has_chat": False, "days": [], "by_card": {}, "counts": {}, "stats": {}, "total": 0}
    today = (datetime.now(MSK) - timedelta(hours=DAY_STARTS)).date()
    lo_day = today - timedelta(days=days)
    if flow["start"]:
        lo_day = max(lo_day, date.fromisoformat(flow["start"]))
    by_card: dict[int, dict[date, str | None]] = {}
    first = None
    for ch in chats:
        who = resolve_senders(conn, flow["name"], ch["chat_id"])
        since = datetime.combine(lo_day, datetime.min.time(), MSK) + timedelta(hours=DAY_STARTS)
        for m in conn.execute("SELECT * FROM chat_messages WHERE chat_id=? AND at>=? ORDER BY at",
                              (ch["chat_id"], since.astimezone(timezone.utc).isoformat())):
            d = day_of(m["at"])
            first = d if first is None or d < first else first
            if d >= today or not is_report(m["text"], m["media"], m["reply"]):
                continue
            card = (who.get(m["sender_id"]) or {}).get("card")
            if card:
                by_card.setdefault(card, {}).setdefault(d, _link(ch["chat_id"], m["msg_id"]))
    # дни считаем с первого прочитанного сообщения: раньше — не знаем, а не «не сдал»
    start = max(lo_day, first) if first else today
    span = [start + timedelta(days=i) for i in range((today - start).days)]
    plist = _people(conn, flow["name"])
    people = [card_id(p["id"]) for p in plist]
    off = days_off(conn, flow["name"])
    rest = {card_id(p["id"]): off.get(p["name"], set()) for p in plist}
    # в календаре «Обзора» выходной без полоски: должников в этот день нет
    counts = {d: sum(1 for c in people if d in by_card.get(c, {})) for d in span if not is_day_off(d)}
    stats = {}
    for c in people:
        mine = by_card.get(c, {})
        # выходной (воскресенье, личный) — не пропуск и не сдача, если не сдал
        work = [d for d in span if d in mine or not (is_day_off(d) or d.weekday() in rest[c])]
        streak = miss = 0
        for d in reversed(work):
            if d in mine:
                if miss:
                    break
                streak += 1
            else:
                if streak:
                    break
                miss += 1
        stats[c] = {"yesterday": bool(work) and work[-1] in mine, "streak": streak, "miss": miss,
                    "done": sum(1 for d in work if d in mine), "of": len(work)}
    return {"has_chat": True, "days": span, "by_card": by_card, "counts": counts,
            "stats": stats, "total": len(people), "today": today,
            "off": {d for d in span if is_day_off(d)}, "rest": rest}


def strangers(conn: sqlite3.Connection, flow: sqlite3.Row, days: int = 14) -> list[dict]:
    """Кто писал отчёты в чат за последние дни, но не узнан как участник."""
    out = []
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    for ch in report_chats(conn, flow["id"]):
        who = resolve_senders(conn, flow["name"], ch["chat_id"])
        ignored = {r["sender_id"] for r in conn.execute(
            "SELECT sender_id FROM chat_senders WHERE how='вручную' AND card IS NULL")}
        seen: dict[int, dict] = {}
        for m in conn.execute("SELECT * FROM chat_messages WHERE chat_id=? AND at>=?", (ch["chat_id"], since)):
            sid = m["sender_id"]
            if not sid or sid in ignored or (who.get(sid) or {}).get("card") \
                    or not is_report(m["text"], m["media"], m["reply"]):
                continue
            e = seen.setdefault(sid, {"sender_id": sid, "sender": m["sender"], "username": m["username"], "n": 0})
            e["n"] += 1
        out += list(seen.values())
    return out
