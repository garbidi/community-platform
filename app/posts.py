"""Посты в чаты потока от аккаунта сообщества.

Черновик → организатор правит → отправляет кнопкой. Сам по себе ничего не
уходит: в чате потока десятки людей, и сообщение пишется от живого аккаунта.
Заготовка собирается из таблиц деклараций (вовлечённые, цели), Claude может
её переписать живым языком — факты берёт только из заготовки.

Писать можно только в чаты, которым в потоке назначена роль (раздел
«Потоки» → «Чаты потока»), — в случайную группу аккаунта пост не попадёт.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import claude_env, flows, store, tg_account
from .config import APP_DB
from .board import inv_date as _when

SCHEMA = """
CREATE TABLE IF NOT EXISTS post_examples (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,           -- сенсационная новость | лидерборд | анонс | …
    text       TEXT NOT NULL,           -- как написал организатор, без правок
    chat_id    INTEGER, msg_id INTEGER, -- откуда взят (ссылка на оригинал)
    at         TEXT, author TEXT,
    added_by   TEXT, added_at TEXT,
    UNIQUE (chat_id, msg_id)
);
CREATE TABLE IF NOT EXISTS posts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_id     INTEGER NOT NULL REFERENCES flows(id),
    kind        TEXT NOT NULL,              -- см. KINDS
    chat_id     INTEGER,
    topic_id    INTEGER NOT NULL DEFAULT 0,  -- тема группы с темами; 0 — общая лента
    chat_title  TEXT,
    text        TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'черновик',  -- черновик|пишется|отправлен|снят
    note        TEXT,                        -- последняя ошибка или пояснение
    created_by  TEXT, created_at TEXT,
    updated_at  TEXT,
    sent_by     TEXT, sent_at TEXT, msg_id INTEGER
);
"""

KINDS = {
    "отчёты": "Отчёты за вчера",
    "вовлечённые": "Вовлечённые за неделю",
    "цели": "Итоги недели по декларациям",
    "свободный": "Свой текст",
}

# Примеры для ИИ: удачные сообщения организаторов из чатов. ИИ пишет посты сам,
# по фактам платформы; примеры задают тон, длину, эмодзи и что принято упоминать.
# Шаблоном для подстановки они не служат.
EXAMPLE_KINDS = ("сенсационная новость", "лидерборд", "анонс", "отчёты")


def examples(conn, kind: str | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM post_examples" + (" WHERE kind=?" if kind else "") + " ORDER BY kind, at DESC"
    return conn.execute(sql, (kind,) if kind else ()).fetchall()


def add_example(conn, kind: str, chat_id: int, msg_id: int, who: str) -> bool:
    m = conn.execute("SELECT * FROM chat_messages WHERE chat_id=? AND msg_id=?", (chat_id, msg_id)).fetchone()
    if not m or not (m["text"] or "").strip():
        return False
    conn.execute("INSERT OR IGNORE INTO post_examples (kind, text, chat_id, msg_id, at, author, added_by, added_at) "
                 "VALUES (?,?,?,?,?,?,?,?)", (kind, m["text"], chat_id, msg_id, m["at"], m["sender"], who, _now()))
    return True


# в какие чаты потока можно писать посты: чеки — служебный учёт, туда не пишем
POSTABLE = ("поток", "команда", "организаторы", "отчёты")


MEDIA = APP_DB.parent / "post_media"      # вложения черновиков: <рядом с базой>/post_media/<пост>/<файл>


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(posts)")}
    for col, typ in (("media", "TEXT"), ("reply_to", "INTEGER")):
        if col not in cols:
            conn.execute(f"ALTER TABLE posts ADD COLUMN {col} {typ}")
    if "topic_id" not in {r[1] for r in conn.execute("PRAGMA table_info(posts)")}:
        conn.execute("ALTER TABLE posts ADD COLUMN topic_id INTEGER NOT NULL DEFAULT 0")
    # перезапуск посреди Claude или отправки: пост не должен зависнуть в «пишется»
    conn.execute("UPDATE posts SET status='черновик', note='прервано перезапуском — проверьте, "
                 "не ушёл ли пост, прежде чем отправлять снова' WHERE status='пишется'")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ----------------------------------------------------------------- чтение

def listing(conn: sqlite3.Connection, flow_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM posts WHERE flow_id=? ORDER BY status='отправлен', "
        "COALESCE(sent_at, updated_at, created_at) DESC", (flow_id,)).fetchall()


def get(conn: sqlite3.Connection, post_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()


def chats_for(conn: sqlite3.Connection, flow_id: int) -> list[sqlite3.Row]:
    return [c for c in flows.chats(conn, flow_id) if c["kind"] in POSTABLE]


# ----------------------------------------------------------------- заготовки

def _first_last(name: str) -> str:
    """«Сидоренко Роман» → «Роман Сидоренко»: в таблицах фамилия первой."""
    parts = (name or "").split()
    return f"{parts[1]} {parts[0]}" if len(parts) == 2 else name


def _team_short(title: str | None) -> str:
    return (title or "Без команды").split("—")[0].strip()


def _people(conn, flow: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT p.*, t.title AS team_title FROM people p LEFT JOIN teams t ON t.id=p.team_id "
        "WHERE p.flow=? AND p.status='в потоке'", (flow,)).fetchall()


def engagement_facts(conn, flow: str, today: date | None = None) -> dict[str, Any]:
    """Вовлечённые по личным планкам из таблиц: кто за неделю, итог, команды."""
    today = today or date.today()
    week_ago = today - timedelta(days=7)
    people = {p["id"]: p for p in _people(conn, flow)}
    fresh, teams, plan, done = [], {}, 0, 0
    for p in people.values():
        rows = conn.execute("SELECT text, done FROM involved WHERE person_id=? ORDER BY idx",
                            (p["id"],)).fetchall()
        mine = [r for r in rows if r["done"]]
        plan += len(rows)
        done += len(mine)
        t = teams.setdefault(_team_short(p["team_title"]), {"done": 0, "plan": 0})
        t["done"] += len(mine)
        t["plan"] += len(rows)
        week = [r for r in mine if (w := _when(r["text"], today)) and week_ago < w <= today]
        if week:
            fresh.append({"name": _first_last(p["name"]), "team": _team_short(p["team_title"]),
                          "week": len(week), "total": len(mine),
                          "over": any("сверх" in (r["text"] or "") for r in week)})
    fresh.sort(key=lambda f: (-f["week"], -f["total"], f["name"]))
    # итоги — как на «Дашборде» таблиц и в «Командах», с выбывшими и долей за выбывших
    from . import board
    fl = flows.get(conn, name=flow)
    if fl:
        b = board.board(conn, fl, today)
        if b["teams"]:
            teams = {t["title"]: {"done": t["done"], "plan": t["goal"] or t["plan"]} for t in b["teams"]}
            done = sum(t["done"] for t in teams.values())
            plan = sum(t["plan"] for t in teams.values())
    return {"flow": flow, "from": week_ago + timedelta(days=1), "to": today,
            "fresh": fresh, "done": done, "plan": plan,
            "teams": dict(sorted(teams.items()))}


def goals_facts(conn, flow: str, today: date | None = None) -> dict[str, Any]:
    """Цели деклараций, закрытые за неделю (по дедлайну), и общий счёт."""
    today = today or date.today()
    week_ago = today - timedelta(days=7)
    people = _people(conn, flow)
    pids = [p["id"] for p in people]
    if not pids:
        return {"flow": flow, "closed": [], "total": 0, "closed_all": 0,
                "from": week_ago + timedelta(days=1), "to": today}
    marks = ",".join("?" * len(pids))
    by = {p["id"]: p for p in people}
    closed = []
    for r in conn.execute(
            f"SELECT person_id, title, deadline FROM decl_goals WHERE person_id IN ({marks}) "
            f"AND COALESCE(progress,0) >= 0.999 AND deadline > ? AND deadline <= ? ORDER BY deadline",
            (*pids, week_ago.isoformat(), today.isoformat())):
        closed.append({"name": _first_last(by[r["person_id"]]["name"]), "title": r["title"]})
    total, closed_all = conn.execute(
        f"SELECT COUNT(*), SUM(COALESCE(progress,0) >= 0.999) FROM decl_goals "
        f"WHERE person_id IN ({marks})", pids).fetchone()
    return {"flow": flow, "closed": closed, "total": total or 0, "closed_all": closed_all or 0,
            "from": week_ago + timedelta(days=1), "to": today}


def _goals_word(n: int) -> str:
    n100, n10 = n % 100, n % 10
    if 11 <= n100 <= 14:
        return "целей"
    return "цель" if n10 == 1 else "цели" if 2 <= n10 <= 4 else "целей"


def _span(f: dict) -> str:
    return f"{f['from']:%d.%m}–{f['to']:%d.%m}"


def draft(conn, flow: str, kind: str) -> str:
    """Заготовка поста из данных. Её правят руками или отдают Claude."""
    if kind == "вовлечённые":
        f = engagement_facts(conn, flow)
        lines = [f"Вовлечённые {flow} · неделя {_span(f)}", ""]
        if f["fresh"]:
            lines.append("За неделю вовлекли:")
            for x in f["fresh"]:
                extra = " — сверх личной планки 🔥" if x["over"] else ""
                lines.append(f"• {x['name']} ({x['team']}) — +{x['week']}, всего {x['total']}{extra}")
        else:
            lines.append("На этой неделе новых вовлечённых в таблицах пока нет.")
        lines += ["", f"Всего по потоку: {f['done']} из {f['plan']}."]
        for name, t in f["teams"].items():
            lines.append(f"{name}: {t['done']} из {t['plan']}")
        left = f["plan"] - f["done"]
        if left > 0:
            lines += ["", f"До закрытия планок — ещё {left}. Кого зовёте на этой неделе?"]
        return "\n".join(lines)
    if kind == "отчёты":
        from . import reports
        from datetime import datetime as _dt
        f = flows.get(conn, name=flow)
        day = (_dt.now(reports.MSK) - timedelta(days=1)).date()
        if not f or not reports.report_chats(conn, f["id"]):
            return "Чат отчётов не выбран: «Потоки» → поток → «Чаты потока» → роль «Отчёты участников»."
        return reports.draft_text(reports.day_report(conn, f, day), flow)
    if kind == "цели":
        f = goals_facts(conn, flow)
        lines = [f"Декларации {flow} · неделя {_span(f)}", ""]
        if f["closed"]:
            # только имена и число: формулировки целей личные (терапия, здоровье,
            # семья) — в общий чат их не выносим, автор сам расскажет, если захочет
            by: dict[str, int] = {}
            for x in f["closed"]:
                by[x["name"]] = by.get(x["name"], 0) + 1
            lines.append("Закрыли цели:")
            for name, n in sorted(by.items(), key=lambda kv: (-kv[1], kv[0])):
                lines.append(f"• {name} — {n} {_goals_word(n)}")
        else:
            lines.append("Закрытых на этой неделе целей в таблицах пока нет.")
        lines += ["", f"Всего закрыто {f['closed_all']} целей из {f['total']}."]
        return "\n".join(lines)
    return ""


# ----------------------------------------------------------------- правка

class PostError(ValueError):
    """Ошибка, которую можно показать человеку как есть."""


def create(conn, flow_id: int, kind: str, user: sqlite3.Row) -> int:
    f = flows.get(conn, flow_id)
    if not f:
        raise PostError("Поток не найден.")
    if kind not in KINDS:
        raise PostError("Неизвестный вид поста.")
    chats = chats_for(conn, flow_id)
    main = next((c for c in chats if c["kind"] == "поток"), chats[0] if chats else None)
    now = _now()
    cur = conn.execute(
        "INSERT INTO posts (flow_id, kind, chat_id, topic_id, chat_title, text, created_by, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (flow_id, kind, main["chat_id"] if main else None, main["topic_id"] if main else 0,
         flows.chat_label(main) if main else None,
         draft(conn, f["name"], kind), user["login"], now, now))
    return int(cur.lastrowid)


def _lines(text: str) -> str:
    """Переносы из браузера приходят как \r\n — телеграм показал бы \r пробелом
    в конце каждой строки (заметили 15.10.2026 в чате «Бот»)."""
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def create_in_chat(conn, flow_id: int, chat_id: int, topic_id: int, text: str, user: sqlite3.Row,
                   reply_to: int | None = None) -> int:
    """Черновик, написанный прямо в чате (раздел «Посты» как телеграм)."""
    c = next((c for c in flows.chats(conn, flow_id) if (c["chat_id"], c["topic_id"]) == (chat_id, topic_id)), None)
    if not c:
        raise PostError("Этого чата нет среди чатов потока.")
    now = _now()
    cur = conn.execute(
        "INSERT INTO posts (flow_id, kind, chat_id, topic_id, chat_title, text, created_by, created_at, updated_at, reply_to) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (flow_id, "свободный", chat_id, topic_id, flows.chat_label(c), _lines(text), user["login"], now, now, reply_to))
    return int(cur.lastrowid)


def media_of(p) -> list[str]:
    try:
        return json.loads(p["media"] or "[]")
    except (ValueError, TypeError, IndexError, KeyError):
        return []


def add_media(conn, post_id: int, name: str, data: bytes) -> None:
    """Приложить картинку к черновику (до 10 — как альбом в телеграме)."""
    p = get(conn, post_id)
    if not p or p["status"] != "черновик":
        raise PostError("Прикладывать можно только к черновику.")
    files = media_of(p)
    if len(files) >= 10:
        raise PostError("В одном сообщении — не больше 10 картинок.")
    ext = (Path(name).suffix or ".jpg").lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        raise PostError("Пока можно прикладывать только картинки (jpg, png, webp).")
    if len(data) > 10 * 1024 * 1024:
        raise PostError("Картинка больше 10 МБ.")
    d = MEDIA / str(post_id)
    d.mkdir(parents=True, exist_ok=True)
    fname = f"{len(files) + 1:02d}{ext}"
    (d / fname).write_bytes(data)
    files.append(fname)
    conn.execute("UPDATE posts SET media=?, updated_at=? WHERE id=?", (json.dumps(files), _now(), post_id))


def drop_media(conn, post_id: int, fname: str) -> None:
    p = get(conn, post_id)
    files = [f for f in media_of(p) if f != fname] if p else []
    if p:
        (MEDIA / str(post_id) / fname).unlink(missing_ok=True)
        conn.execute("UPDATE posts SET media=? WHERE id=?", (json.dumps(files), post_id))


def avatar_of(conn, card: int) -> int | None:
    """Телеграм участника — для фото из его аватарки."""
    r = conn.execute("SELECT sender_id FROM chat_senders WHERE card=? ORDER BY how='вручную' DESC LIMIT 1",
                     (card,)).fetchone()
    if r:
        return r["sender_id"]
    p = conn.execute("SELECT tg_user_id FROM people WHERE id=?", (-card,)).fetchone()
    return p["tg_user_id"] if p and p["tg_user_id"] else None


def avatar_file(conn, card: int) -> Path:
    """Фото того, кто вовлёк: своё фото на платформе, иначе аватарка в телеграме."""
    from .fourday import photo_path
    own = photo_path(-card) if card < 0 else None
    if own and own.exists():                         # фото участника на платформе — лучше аватарки
        return own
    uid = avatar_of(conn, card)
    if not uid:
        raise PostError("Не знаю телеграм этого участника — он ещё не писал в чатах потока.")
    tmp = MEDIA / f"avatar_{uid}.jpg"
    MEDIA.mkdir(parents=True, exist_ok=True)
    if not tmp.exists() and not tg_account.fetch_avatar_big(uid, str(tmp)):
        raise PostError("У участника нет аватарки в телеграме (или она скрыта).")
    return tmp


def add_avatar(conn, post_id: int, card: int) -> None:
    """Фото к «Сенсационной новости» — аватарка в телеграме того, кто вовлёк."""
    add_media(conn, post_id, "avatar.jpg", avatar_file(conn, card).read_bytes())


def create_text(conn, flow_id: int, kind: str, text: str, note: str | None = None) -> int:
    """Черновик, собранный платформой сама (утренние отчёты). Чат — чат отчётов,
    если есть, иначе основной чат потока; отправляет организатор кнопкой."""
    chats = chats_for(conn, flow_id)
    main = next((c for c in chats if c["kind"] == "отчёты"), None) \
        or next((c for c in chats if c["kind"] == "поток"), chats[0] if chats else None)
    now = _now()
    cur = conn.execute(
        "INSERT INTO posts (flow_id, kind, chat_id, topic_id, chat_title, text, note, created_by, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (flow_id, kind, main["chat_id"] if main else None, main["topic_id"] if main else 0,
         flows.chat_label(main) if main else None, text, note, "платформа", now, now))
    return int(cur.lastrowid)


def _target(conn, flow_id: int, chat: str) -> sqlite3.Row | None:
    """«<чат>:<тема>» из формы → выбранный чат потока или None."""
    m = re.fullmatch(r"(-?\d+):(\d+)", chat or "")
    if not m:
        return None
    key = (int(m.group(1)), int(m.group(2)))
    return next((c for c in chats_for(conn, flow_id) if (c["chat_id"], c["topic_id"]) == key), None)


def update(conn, post_id: int, text: str, chat: str) -> None:
    p = get(conn, post_id)
    if not p or p["status"] != "черновик":
        raise PostError("Править можно только черновик.")
    c = _target(conn, p["flow_id"], chat) if chat else None
    if chat and not c:
        raise PostError("Этого чата нет среди чатов потока.")
    conn.execute("UPDATE posts SET text=?, chat_id=?, topic_id=?, chat_title=?, note=NULL, updated_at=? WHERE id=?",
                 (_lines(text), c["chat_id"] if c else None, c["topic_id"] if c else 0,
                  flows.chat_label(c) if c else None, _now(), post_id))


def delete(conn, post_id: int) -> None:
    p = get(conn, post_id)
    if p and p["status"] == "отправлен":
        raise PostError("Пост уже в чате — сначала удалите его из чата.")
    conn.execute("DELETE FROM posts WHERE id=?", (post_id,))


def send(conn, post_id: int, user: sqlite3.Row) -> None:
    p = get(conn, post_id)
    if not p or p["status"] != "черновик":
        raise PostError("Отправить можно только черновик.")
    if not (p["text"] or "").strip() and not media_of(p):
        raise PostError("Пост пустой.")
    if len(p["text"]) > 4000:
        raise PostError("Длиннее 4000 знаков — телеграм такой не примет. Сократите.")
    if media_of(p) and len(p["text"]) > 4000:
        raise PostError("Подпись к картинкам длиннее 4000 знаков — сократите.")
    if not any((c["chat_id"], c["topic_id"]) == (p["chat_id"], p["topic_id"])
               for c in chats_for(conn, p["flow_id"])):
        raise PostError("Выберите чат потока.")
    # «пишется» на время отправки: второй клик не отправит пост дважды
    took = conn.execute("UPDATE posts SET status='пишется' WHERE id=? AND status='черновик'",
                        (post_id,)).rowcount
    if not took:
        raise PostError("Пост уже отправляется.")
    files = [str(MEDIA / str(post_id) / f) for f in media_of(p)]
    res = tg_account.send(p["chat_id"], p["text"], p["topic_id"], files=files or None,
                          reply_to=p["reply_to"])
    if not res.get("ok"):
        conn.execute("UPDATE posts SET status='черновик', note=? WHERE id=?",
                     (res.get("error") or "Не отправилось.", post_id))
        raise PostError(res.get("error") or "Не отправилось.")
    conn.execute("UPDATE posts SET status='отправлен', note=NULL, sent_by=?, sent_at=?, msg_id=? WHERE id=?",
                 (user["login"], _now(), res["msg_id"], post_id))


def unsend(conn, post_id: int) -> None:
    p = get(conn, post_id)
    if not p or p["status"] != "отправлен" or not p["msg_id"]:
        raise PostError("Этот пост не в чате.")
    res = tg_account.unsend(p["chat_id"], p["msg_id"])
    if not res.get("ok"):
        raise PostError(res.get("error") or "Не удалилось.")
    conn.execute("UPDATE posts SET status='снят', note=?, updated_at=? WHERE id=?",
                 (f"удалён из чата {_now()[:16].replace('T', ' ')}", _now(), post_id))


# ----------------------------------------------------------------- Claude

PROMPT = """Ты помогаешь организаторам мастермайнда «Вершина» писать посты в телеграм-чат потока {flow}.
Перепиши заготовку ниже в живой пост для участников: тепло, по-человечески, коротко,
без канцелярита и без пафоса. Можно 1–3 эмодзи. Обращение на «вы» ко всем, по имени — к отмеченным.

Строго:
- факты, имена и числа — только из заготовки, ничего не придумывай и не округляй;
- никого не стыди и не называй тех, у кого не получилось;
- не раскрывай личного: здоровье, терапию, семью, деньги конкретного человека — даже если это есть в заготовке;
- без markdown-разметки (звёздочки, решётки) — телеграм покажет её как есть;
- не длиннее 1200 знаков;
- ответь только текстом поста, без пояснений.

Заготовка:
{text}
"""


def rewrite(post_id: int) -> None:
    """Переписать черновик через Claude в фоне: статус «пишется», потом снова черновик."""
    conn = store.connect()
    try:
        p = get(conn, post_id)
        if not p or p["status"] != "черновик":
            return
        f = flows.get(conn, p["flow_id"])
        conn.execute("UPDATE posts SET status='пишется', note=NULL WHERE id=?", (post_id,))
    finally:
        conn.close()

    def run():
        import subprocess
        c = store.connect()
        try:
            try:
                done = subprocess.run(["claude", "-p"], input=PROMPT.format(flow=f["name"], text=p["text"]),
                                      capture_output=True, text=True, timeout=300, env=claude_env.env())
                out = (done.stdout or "").strip()
                if done.returncode != 0 or not out or "Not logged in" in out:
                    raise RuntimeError(claude_env.explain((done.stderr or out or "пустой ответ").strip()[:400]))
                c.execute("UPDATE posts SET status='черновик', text=?, note=?, updated_at=? WHERE id=?",
                          (out, "переписан Claude — проверьте факты перед отправкой", _now(), post_id))
            except Exception as exc:          # noqa: BLE001 — любую ошибку показываем в посте
                c.execute("UPDATE posts SET status='черновик', note=? WHERE id=?",
                          (f"Claude не переписал: {exc}", post_id))
        finally:
            c.close()
    threading.Thread(target=run, daemon=True).start()


# ----------------------------------------------------------------- генерация

# Что умеет «✨ Сгенерировать» в «Постах»: вид → какие примеры брать для стиля.
GEN_KINDS = {
    "сенсационная новость": "Сенсационная новость",
    "лидерборд": "Лидерборд",
    "отчёты": "Сводка отчётов",
    "анонс": "Анонс / свой текст",
}

GEN_PROMPT = """Ты пишешь пост для телеграм-чата мастермайнда «Вершина», поток {flow}, от имени организаторов.
Вид поста: {kind}.

Ниже — настоящие посты организатора этого вида. Повтори их стиль: длину, строение,
эмодзи, заголовки, обороты, обращение. Не копируй их дословно и не бери из них факты.

Строго:
- факты, имена, ники и числа — только из блока «Факты» и из пожелания организатора; ничего не придумывай и не округляй;
- исключение — названия групп (например, «Коллективный разум»): их можно взять из примеров, сопоставив группу по менторам;
- если в фактах чего-то нет (например, данных о выбывших или % целей) — просто не упоминай это;
- никого не стыди; не раскрывай личного: здоровье, терапию, семью, деньги конкретного человека;
- без markdown (звёздочек, решёток) — телеграм покажет их как есть;
- фирменные буквы сообщества пишутся так: [[ММ7]], [[ЛИДЕРБОРД]], [[ДЕНЬ|зелёный]] — используй их там же, где они стоят в примерах;
- ответь только текстом поста, без пояснений и кавычек.

Примеры:
{examples}

Факты (на {today}):
{facts}

Пожелание организатора: {hint}
"""


def _nicks(conn, flow: str) -> dict[int, str]:
    """person_id → @ник из чатов: в таблицах ников нет, авторы узнаны по сообщениям."""
    out = {}
    for p in _people(conn, flow):
        if p["username"]:
            out[p["id"]] = p["username"]
    for r in conn.execute(
            "SELECT s.card, MAX(m.username) u FROM chat_senders s JOIN chat_messages m "
            "ON m.sender_id=s.sender_id WHERE m.username IS NOT NULL AND s.card < 0 GROUP BY s.card"):
        out.setdefault(-r["card"], r["u"])
    return out


def gen_facts(conn, f: sqlite3.Row, kind: str, today: date | None = None) -> str:
    """Факты для Claude под вид поста — простым текстом."""
    from . import board, reports
    today = today or date.today()
    flow = f["name"]
    if kind == "отчёты":
        if not reports.report_chats(conn, f["id"]):
            raise PostError("Чат отчётов не выбран: «Потоки» → поток → «Чаты потока».")
        day = (datetime.now(reports.MSK) - timedelta(days=1)).date()
        r = reports.day_report(conn, f, day)
        miss = ", ".join(m["name"] for m in r["missing"]) or "все сдали"
        return f"Отчёты за {day:%d.%m}: сдали {r['done']} из {r['total']}. Не сдали: {miss}."
    if kind == "анонс":
        return f"Поток {flow}. Всё содержание анонса — в пожелании организатора."
    b = board.board(conn, f, today)
    lines = []
    if kind == "лидерборд":
        goal = b["goal"] or b["plan"]
        lines.append(f"Дата: {today.day} {_MONTHS[today.month - 1]}")
        lines.append(f"Вовлечение потока: планка {goal}, вовлекли {b['done']}"
                     + (f" ({b['done'] / goal * 100:.2f}%)".replace(".", ",") if goal else ""))
        pr = [p["progress"] for p in _people(conn, flow) if p["progress"] is not None]
        if pr:
            lines.append(f"Цели потока (средний прогресс по таблицам): {sum(pr) / len(pr) * 100:.1f}%".replace(".", ","))
        for t in b["teams"]:
            who = " + ".join(n.split()[0] for n in t["mentors"]) or "—"
            chat = f", чат «{t['chat']['title']}»" if t["chat"] else ""
            lines.append(f"Группа {t['title']} (менторы: {who}{chat}): планка {t['goal'] or t['plan']}, "
                         f"вовлекли {t['done']}, за неделю +{t['week']}"
                         + (f", место {t['place']}" if t["place"] else ""))
        return "\n".join(lines)
    # сенсационная новость: свежие вовлечения с никами — кого поздравлять
    nicks = _nicks(conn, flow)
    recent, totals = [], []
    for p in _people(conn, flow):
        done = [r["text"] for r in conn.execute(
            "SELECT text FROM involved WHERE person_id=? AND done=1 ORDER BY idx", (p["id"],))]
        if not done:
            continue
        totals.append((len(done), _first_last(p["name"])))
        for n, txt in enumerate(done, 1):
            d = _when(txt, today)
            if d and today - timedelta(days=3) <= d <= today:
                recent.append((d, n, len(done), p, txt))
    recent.sort(key=lambda x: (x[0], x[1]), reverse=True)
    if recent:
        lines.append("Вовлечения за последние 3 дня (свежие сверху):")
        for d, n, total, p, txt in recent:
            nick = f"@{nicks[p['id']]}" if p["id"] in nicks else "ника нет"
            over = ", сверх личной планки" if "сверх" in txt.lower() else ""
            lines.append(f"- {d:%d.%m}: {_first_last(p['name'])} ({nick}, {p['team_title'] or 'без команды'}) — "
                         f"{n}-е вовлечение, всего у него/неё {total}{over}")
        same = sum(1 for x in recent if x[0] == recent[0][0])
        lines.append(f"Всего вовлечений {recent[0][0]:%d.%m}: {same}.")
    else:
        lines.append("За последние 3 дня новых вовлечений в таблицах нет.")
    totals.sort(reverse=True)
    if totals:
        top = totals[0][0]
        lines.append("Больше всех вовлечений: " + ", ".join(n for c, n in totals if c == top) + f" — по {top}.")
    lines.append(f"Поток: вовлекли {b['done']}" + (f" из {b['goal']}" if b["goal"] else "") + ".")
    return "\n".join(lines)


_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
           "сентября", "октября", "ноября", "декабря")


def generate(conn, flow_id: int, kind: str, hint: str = "", facts: str | None = None) -> str:
    """Пост целиком от Claude: факты платформы + примеры организатора для стиля.
    Ничего не сохраняет — текст уходит в поле ввода, организатор правит."""
    import subprocess
    f = flows.get(conn, flow_id)
    if not f:
        raise PostError("Поток не найден.")
    if kind not in GEN_KINDS:
        raise PostError("Неизвестный вид поста.")
    if kind == "анонс" and not hint.strip():
        raise PostError("Для анонса напишите в поле, о чём он: что, когда, для кого.")
    if not claude_env.token():
        raise PostError(claude_env.NOT_LOGGED)
    ex = examples(conn, kind)[:6]
    shown = "\n\n".join(f"--- пример {i} ---\n{e['text'].strip()}" for i, e in enumerate(ex, 1)) \
        or "(примеров этого вида пока нет — пиши коротко и тепло)"
    prompt = GEN_PROMPT.format(flow=f["name"], kind=GEN_KINDS[kind], examples=shown,
                               today=f"{date.today():%d.%m.%Y}", facts=facts or gen_facts(conn, f, kind),
                               hint=hint.strip() or "нет — выбери самое свежее событие из фактов")
    try:
        done = subprocess.run(["claude", "-p"], input=prompt, capture_output=True, text=True,
                              timeout=180, env=claude_env.env(), cwd="/tmp")
    except subprocess.TimeoutExpired:
        raise PostError("Claude не ответил за 3 минуты — попробуйте ещё раз.")
    out = (done.stdout or "").strip()
    if done.returncode != 0 or not out:
        raise PostError(claude_env.explain((done.stderr or out or "пустой ответ").strip()[:400]))
    low = out.lower()
    if "not logged in" in low or "/login" in low:
        raise PostError(claude_env.NOT_LOGGED)
    return out
