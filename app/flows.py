"""Потоки: создают и ведут сами организаторы, без разработчика.

Поток — это название, даты, команды (у команды ММ7 своя Google-таблица
деклараций; команда без таблицы ведётся на платформе — app/decl.py), менторы и трекеры команд, чаты потока в телеграме. Всё это
живёт в базе и правится со страницы «Потоки». Раньше поток задавался
файлом mm7.json на сервере; при первом запуске файл переносится сюда
и дальше не читается.

Жизнь потока: готовится → идёт → завершён.
- «готовится»: таблицы уже читаются раз в час — можно проверить, что всё
  подтягивается, до старта;
- «идёт»: то же, плюс поток может быть текущим — его показывают «Обзор»,
  «Поток» и «Участники» по умолчанию;
- «завершён»: таблицы больше не читаются, данные замирают и остаются
  историей людей (архив). Вернуть в работу можно — на случай ошибки.

Текущий поток ровно один или ни одного.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

STATUSES = ("готовится", "идёт", "завершён")

_FLOW_CHATS = """
CREATE TABLE IF NOT EXISTS flow_chats (
    flow_id     INTEGER NOT NULL REFERENCES flows(id),
    chat_id     INTEGER NOT NULL,
    title       TEXT,
    kind        TEXT NOT NULL DEFAULT 'поток',
    team_id     INTEGER REFERENCES flow_teams(id),
    topic_id    INTEGER NOT NULL DEFAULT 0,   -- тема группы с темами; 0 — вся группа
    topic_title TEXT,
    PRIMARY KEY (flow_id, chat_id, topic_id)
);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS flows (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,            -- «ММ7»
    start       TEXT,                            -- ГГГГ-ММ-ДД
    finish      TEXT,
    status      TEXT NOT NULL DEFAULT 'готовится',
    is_current  INTEGER NOT NULL DEFAULT 0,
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS flow_teams (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_id     INTEGER NOT NULL REFERENCES flows(id),
    no          INTEGER,
    title       TEXT NOT NULL,                   -- «Команда 1 — Камила, Глеб и Вика Белая»
    sheet_id    TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (flow_id, title)
);

-- менторы и трекеры команды; ментор видит участников своей команды
CREATE TABLE IF NOT EXISTS flow_staff (
    flow_team_id INTEGER NOT NULL REFERENCES flow_teams(id),
    user_id      INTEGER NOT NULL REFERENCES users(id),
    role         TEXT NOT NULL DEFAULT 'ментор',   -- ментор | трекер
    PRIMARY KEY (flow_team_id, user_id)
);

-- чаты потока в телеграме: что читать аккаунтом сообщества

"""

SHEET_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})")


def init(conn: sqlite3.Connection, legacy_config: Path | None = None) -> None:
    conn.executescript(SCHEMA)
    conn.executescript(_FLOW_CHATS)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(flow_chats)")}
    if "topic_id" not in cols:
        # темы групп: ключ теперь (поток, чат, тема) — таблицу пересобираем
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("ALTER TABLE flow_chats RENAME TO flow_chats_old")
            conn.execute(_FLOW_CHATS)
            kind = "kind" if "kind" in cols else "'поток'"
            team = "team_id" if "team_id" in cols else "NULL"
            conn.execute(f"INSERT INTO flow_chats (flow_id, chat_id, title, kind, team_id) "
                         f"SELECT flow_id, chat_id, title, {kind}, {team} FROM flow_chats_old")
            conn.execute("DROP TABLE flow_chats_old")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    if "goal" not in {r[1] for r in conn.execute("PRAGMA table_info(flow_teams)")}:
        # цель команды на поток — сколько человек вовлечь (раздел «Команды»)
        conn.execute("ALTER TABLE flow_teams ADD COLUMN goal INTEGER")
    if legacy_config and not conn.execute("SELECT 1 FROM flows LIMIT 1").fetchone():
        _import_legacy(conn, legacy_config)


def _import_legacy(conn: sqlite3.Connection, path: Path) -> None:
    """mm7.json → таблицы. Один раз, при первом запуске новой версии."""
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    cur = conn.execute(
        "INSERT INTO flows (name, start, finish, status, is_current, note) VALUES (?,?,?,?,1,?)",
        (cfg["flow"], cfg.get("start"), cfg.get("end"), "идёт",
         (cfg.get("note") or "") + " Перенесено из mm7.json."))
    fid = int(cur.lastrowid)
    for t in cfg.get("teams", []):
        tid = int(conn.execute(
            "INSERT INTO flow_teams (flow_id, no, title, sheet_id) VALUES (?,?,?,?)",
            (fid, t.get("no"), t["title"], t["sheet"])).lastrowid)
        for role, key in (("ментор", "mentors"), ("трекер", "trackers")):
            for login in t.get(key, []):
                u = conn.execute("SELECT id FROM users WHERE login=?", (login.lower(),)).fetchone()
                if u:
                    conn.execute("INSERT OR IGNORE INTO flow_staff VALUES (?,?,?)", (tid, u["id"], role))


# ----------------------------------------------------------------- чтение

def sheet_id_of(link: str) -> str | None:
    """Ссылка на таблицу или голый id → id."""
    link = (link or "").strip()
    m = SHEET_RE.search(link)
    if m:
        return m.group(1)
    return link if re.fullmatch(r"[A-Za-z0-9_-]{20,}", link) else None


def get(conn: sqlite3.Connection, flow_id: int | None = None,
        name: str | None = None) -> sqlite3.Row | None:
    if flow_id is not None:
        return conn.execute("SELECT * FROM flows WHERE id=?", (flow_id,)).fetchone()
    if name is not None:
        return conn.execute("SELECT * FROM flows WHERE name=?", (name,)).fetchone()
    return None


def current(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM flows WHERE is_current=1").fetchone()


def listing(conn: sqlite3.Connection) -> list[dict]:
    """Все потоки: текущий первым, потом идущие, готовящиеся, завершённые."""
    order = {"идёт": 1, "готовится": 2, "завершён": 3}
    rows = conn.execute("SELECT * FROM flows").fetchall()
    out = []
    for f in rows:
        teams = conn.execute("SELECT COUNT(*) FROM flow_teams WHERE flow_id=?", (f["id"],)).fetchone()[0]
        people = conn.execute(
            "SELECT COUNT(*) FROM people WHERE flow=? AND status='в потоке'", (f["name"],)).fetchone()[0]
        sync = conn.execute("SELECT * FROM flow_sync WHERE flow=?", (f["name"],)).fetchone()
        out.append(dict(f, teams=teams, people=people, sync=sync))
    out.sort(key=lambda f: (not f["is_current"], order.get(f["status"], 9),
                            -(int(f["id"]))))
    return out


def teams(conn: sqlite3.Connection, flow_id: int) -> list[dict]:
    out = []
    for t in conn.execute("SELECT * FROM flow_teams WHERE flow_id=? ORDER BY COALESCE(no, 999), title",
                          (flow_id,)):
        staff = conn.execute(
            "SELECT s.role, u.id, u.login, u.name, u.active FROM flow_staff s "
            "JOIN users u ON u.id=s.user_id WHERE s.flow_team_id=? ORDER BY s.role, u.name",
            (t["id"],)).fetchall()
        out.append(dict(t, mentors=[s for s in staff if s["role"] == "ментор"],
                        trackers=[s for s in staff if s["role"] == "трекер"]))
    return out


def config(conn: sqlite3.Connection, flow: sqlite3.Row | None) -> dict[str, Any] | None:
    """Поток в форме, которую понимает сборщик таблиц (roster)."""
    if flow is None:
        return None
    ts = teams(conn, flow["id"])
    return {
        "id": flow["id"], "flow": flow["name"], "start": flow["start"], "end": flow["finish"],
        "status": flow["status"],
        "teams": [{"no": t["no"], "title": t["title"], "sheet": t["sheet_id"],
                   "mentors": [s["login"] for s in t["mentors"]],
                   "trackers": [s["login"] for s in t["trackers"]]} for t in ts],
    }


def syncable(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Потоки, таблицы которых читаем: идущие и готовящиеся."""
    return conn.execute("SELECT * FROM flows WHERE status IN ('готовится','идёт') "
                        "ORDER BY is_current DESC, id").fetchall()


# Роль чата в потоке. Участники и организаторы — разные голоса: сообщения
# участников идут в их вовлечение и портрет, организаторские — нет.
CHAT_KINDS = {
    "поток": "Участники — весь поток",
    "команда": "Участники — одна команда",
    "организаторы": "Организаторы и менторы",
    "отчёты": "Отчёты участников",
    "чеки": "Чеки и оплаты",
}


def chats(conn: sqlite3.Connection, flow_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT c.*, t.title AS team_title FROM flow_chats c "
        "LEFT JOIN flow_teams t ON t.id=c.team_id WHERE c.flow_id=? "
        "ORDER BY c.kind, c.title, c.topic_id",
        (flow_id,)).fetchall()


def chat_elsewhere(conn: sqlite3.Connection, flow_id: int) -> dict[int, list[str]]:
    """В каких других потоках уже выбран чат — чтобы не путать ММ7 и ММ8."""
    out: dict[int, list[str]] = {}
    for r in conn.execute(
            "SELECT DISTINCT c.chat_id, f.name FROM flow_chats c JOIN flows f ON f.id=c.flow_id "
            "WHERE c.flow_id<>?", (flow_id,)):
        out.setdefault(r["chat_id"], []).append(r["name"])
    return out


# ----------------------------------------------------------------- правка

class FlowError(ValueError):
    """Ошибка, которую можно показать человеку как есть."""


def _clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", (name or "")).strip()
    if not name:
        raise FlowError("Укажите название потока, например «ММ8».")
    if len(name) > 40:
        raise FlowError("Название слишком длинное — хватит короткого, например «ММ8».")
    return name


def _clean_date(value: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise FlowError("Дата должна быть в виде ГГГГ-ММ-ДД.")
    return value


def create(conn: sqlite3.Connection, name: str, start: str = "", finish: str = "", kind: str = "поток") -> int:
    name = _clean_name(name)
    if get(conn, name=name):
        raise FlowError(f"Поток «{name}» уже есть.")
    s, f = _clean_date(start), _clean_date(finish)
    if s and f and f < s:
        raise FlowError("Финиш раньше старта.")
    return int(conn.execute("INSERT INTO flows (name, start, finish, kind) VALUES (?,?,?,?)",
                            (name, s, f, kind if kind in ("поток", "4 дня") else "поток")).lastrowid)


def update(conn: sqlite3.Connection, flow_id: int, name: str, start: str, finish: str,
           note: str = "") -> None:
    """Название и даты. Переименование тянет за собой участников и команды:
    они привязаны к потоку по названию."""
    f = get(conn, flow_id)
    if not f:
        raise FlowError("Поток не найден.")
    new = _clean_name(name)
    s, e = _clean_date(start), _clean_date(finish)
    if s and e and e < s:
        raise FlowError("Финиш раньше старта.")
    if new != f["name"]:
        if get(conn, name=new):
            raise FlowError(f"Поток «{new}» уже есть.")
        for table in ("people", "teams", "flow_sync"):
            conn.execute(f"UPDATE {table} SET flow=? WHERE flow=?", (new, f["name"]))
    conn.execute("UPDATE flows SET name=?, start=?, finish=?, note=? WHERE id=?",
                 (new, s, e, note.strip() or None, flow_id))


def set_status(conn: sqlite3.Connection, flow_id: int, status: str) -> None:
    if status not in STATUSES:
        raise FlowError("Неизвестное состояние потока.")
    f = get(conn, flow_id)
    if not f:
        raise FlowError("Поток не найден.")
    if status == "завершён":
        conn.execute("UPDATE flows SET status='завершён', is_current=0, finished_at=datetime('now') "
                     "WHERE id=?", (flow_id,))
        # группы потока уходят в архив вместе с ним
        conn.execute("UPDATE teams SET archived=1 WHERE flow=?", (f["name"],))
    else:
        conn.execute("UPDATE flows SET status=?, finished_at=NULL WHERE id=?", (status, flow_id))
        conn.execute("UPDATE teams SET archived=0 WHERE flow=?", (f["name"],))


def make_current(conn: sqlite3.Connection, flow_id: int) -> None:
    f = get(conn, flow_id)
    if not f:
        raise FlowError("Поток не найден.")
    if f["status"] == "завершён":
        raise FlowError("Завершённый поток не может быть текущим — сначала верните его в работу.")
    conn.execute("UPDATE flows SET is_current=0")
    conn.execute("UPDATE flows SET is_current=1, status='идёт' WHERE id=?", (flow_id,))


def add_team(conn: sqlite3.Connection, flow_id: int, title: str, link: str,
             mentors: list[int], trackers: list[int], no: int | None = None) -> int:
    title = re.sub(r"\s+", " ", (title or "")).strip()
    if not title:
        raise FlowError("Укажите название команды.")
    # без ссылки — команда ведётся на платформе (декларации, шаги, вовлечения — app/decl.py)
    sid = sheet_id_of(link) if (link or "").strip() else ""
    if sid is None:
        raise FlowError("Не похоже на ссылку на Google-таблицу. Скопируйте адрес из строки браузера "
                        "или оставьте поле пустым — тогда команда ведётся на платформе.")
    if conn.execute("SELECT 1 FROM flow_teams WHERE flow_id=? AND title=?", (flow_id, title)).fetchone():
        raise FlowError(f"Команда «{title}» в этом потоке уже есть.")
    if no is None:
        no = (conn.execute("SELECT MAX(no) FROM flow_teams WHERE flow_id=?", (flow_id,)).fetchone()[0] or 0) + 1
    tid = int(conn.execute("INSERT INTO flow_teams (flow_id, no, title, sheet_id) VALUES (?,?,?,?)",
                           (flow_id, no, title, sid)).lastrowid)
    set_staff(conn, tid, mentors, trackers)
    if not sid:
        from . import decl
        decl._legacy_team(conn, get(conn, flow_id)["name"], title)
    return tid


def update_team(conn: sqlite3.Connection, team_id: int, title: str, link: str,
                mentors: list[int], trackers: list[int], no: int | None) -> None:
    t = conn.execute("SELECT * FROM flow_teams WHERE id=?", (team_id,)).fetchone()
    if not t:
        raise FlowError("Команда не найдена.")
    title = re.sub(r"\s+", " ", (title or "")).strip() or t["title"]
    sid = sheet_id_of(link) if link.strip() else t["sheet_id"]
    if not sid:
        raise FlowError("Не похоже на ссылку на Google-таблицу.")
    if title != t["title"]:
        f = get(conn, t["flow_id"])
        # группа на странице «Группы» связана с командой потока по названию
        conn.execute("UPDATE teams SET title=? WHERE flow=? AND title=?", (title, f["name"], t["title"]))
    conn.execute("UPDATE flow_teams SET title=?, sheet_id=?, no=? WHERE id=?",
                 (title, sid, no if no is not None else t["no"], team_id))
    set_staff(conn, team_id, mentors, trackers)


def set_goal(conn: sqlite3.Connection, team_id: int, goal: int | None) -> None:
    """Цель команды по вовлечённым на поток; None — не задана."""
    if goal is not None and not 0 < goal <= 10000:
        raise FlowError("Цель — сколько человек команда вовлечёт за поток, число больше нуля.")
    conn.execute("UPDATE flow_teams SET goal=? WHERE id=?", (goal, team_id))


def set_staff(conn: sqlite3.Connection, team_id: int, mentors: list[int], trackers: list[int]) -> None:
    conn.execute("DELETE FROM flow_staff WHERE flow_team_id=?", (team_id,))
    for uid in mentors:
        conn.execute("INSERT OR IGNORE INTO flow_staff VALUES (?,?, 'ментор')", (team_id, uid))
    for uid in trackers:
        conn.execute("INSERT OR IGNORE INTO flow_staff VALUES (?,?, 'трекер')", (team_id, uid))


def remove_team(conn: sqlite3.Connection, team_id: int) -> None:
    """Убрать команду из потока. Участники останутся в базе, но при следующем
    обновлении получат пометку «нет в таблицах» и пропадут из списков потока."""
    conn.execute("DELETE FROM flow_staff WHERE flow_team_id=?", (team_id,))
    # чат убранной команды остаётся в потоке — как чат участников потока
    conn.execute("UPDATE flow_chats SET kind='поток', team_id=NULL WHERE team_id=?", (team_id,))
    conn.execute("DELETE FROM flow_teams WHERE id=?", (team_id,))


def set_chats(conn: sqlite3.Connection, flow_id: int, picked: list[dict]) -> None:
    """picked — {chat_id, topic_id (0 — вся группа), title, topic_title, kind, team_id}.
    У группы с темами роль темы важнее роли всей группы: роль группы —
    для тем, которым своя не задана."""
    teams = {t["id"] for t in conn.execute("SELECT id FROM flow_teams WHERE flow_id=?", (flow_id,))}
    for p in picked:
        if p["kind"] not in CHAT_KINDS:
            raise FlowError("Неизвестная роль чата.")
        if p["kind"] == "команда" and p.get("team_id") not in teams:
            raise FlowError("Для чата команды выберите команду этого потока.")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM flow_chats WHERE flow_id=?", (flow_id,))
        for p in picked:
            conn.execute(
                "INSERT OR IGNORE INTO flow_chats (flow_id, chat_id, title, kind, team_id, topic_id, topic_title) "
                "VALUES (?,?,?,?,?,?,?)",
                (flow_id, p["chat_id"], p.get("title") or "", p["kind"],
                 p.get("team_id") if p["kind"] == "команда" else None,
                 int(p.get("topic_id") or 0), p.get("topic_title") if p.get("topic_id") else None))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def remap_migrated(conn: sqlite3.Connection, migrated: dict) -> int:
    """Роли, выбранные на старой копии группы, переносим на её супергруппу.
    migrated — {старый id: новый id} из списка чатов аккаунта."""
    moved = 0
    for old, new in (migrated or {}).items():
        old, new = int(old), int(new)
        for r in conn.execute("SELECT * FROM flow_chats WHERE chat_id=?", (old,)).fetchall():
            taken = conn.execute("SELECT 1 FROM flow_chats WHERE flow_id=? AND chat_id=? AND topic_id=?",
                                 (r["flow_id"], new, r["topic_id"])).fetchone()
            if taken:      # у новой группы роль уже есть — старую просто убираем
                conn.execute("DELETE FROM flow_chats WHERE flow_id=? AND chat_id=? AND topic_id=?",
                             (r["flow_id"], old, r["topic_id"]))
            else:
                conn.execute("UPDATE flow_chats SET chat_id=? WHERE flow_id=? AND chat_id=? AND topic_id=?",
                             (new, r["flow_id"], old, r["topic_id"]))
            moved += 1
    return moved


def chat_label(c) -> str:
    """«Группа › Тема» для выбранного чата или темы."""
    return f"{c['title']} › {c['topic_title']}" if c["topic_id"] else (c["title"] or str(c["chat_id"]))


def delete(conn: sqlite3.Connection, flow_id: int) -> None:
    """Удалить поток, созданный по ошибке. Только пока в нём нет участников:
    поток с людьми не удаляют, а завершают — это их история."""
    f = get(conn, flow_id)
    if not f:
        raise FlowError("Поток не найден.")
    if conn.execute("SELECT 1 FROM people WHERE flow=? LIMIT 1", (f["name"],)).fetchone():
        raise FlowError("В потоке уже есть участники — его можно только завершить.")
    for t in conn.execute("SELECT id FROM flow_teams WHERE flow_id=?", (flow_id,)).fetchall():
        remove_team(conn, t["id"])
    conn.execute("DELETE FROM flow_chats WHERE flow_id=?", (flow_id,))
    conn.execute("DELETE FROM flow_sync WHERE flow=?", (f["name"],))
    conn.execute("DELETE FROM teams WHERE flow=?", (f["name"],))
    conn.execute("DELETE FROM flows WHERE id=?", (flow_id,))
