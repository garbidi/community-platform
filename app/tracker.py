"""Трекер: где сейчас участник — для ментора и для него самого.

Собирает в одно то, что разложено по разным местам: % декларации, шаги на
неделю и просроченные, отчёты, свежая заметка со созвона (состояние, как с
человеком говорить, что обещал и что из прошлого обещанного сделал).

Ментор видит всё по своей команде (экран команды). Участник — только своё и
в бережной форме: заметку «для участника», а не ментору, без тревожных
сигналов и подсказок, как на него давить.

Участник сам отмечает шаги (вариант, который выбрала Марья): шаг засчитан
сразу, а ментор и трекер видят «отметил сам — проверьте» и подтверждают.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

from . import call_digest

STATE_ORDER = {"выпадает": 0, "буксует": 1, "идёт": 2, None: 3}


def init(conn: sqlite3.Connection) -> None:
    if "self_marked" not in {r[1] for r in conn.execute("PRAGMA table_info(decl_steps)")}:
        conn.execute("ALTER TABLE decl_steps ADD COLUMN self_marked INTEGER NOT NULL DEFAULT 0")


def _jl(v) -> list:
    try:
        x = json.loads(v or "[]")
    except ValueError:
        return []
    return x if isinstance(x, list) else []


def steps(conn: sqlite3.Connection, pid: int, today: date | None = None) -> dict:
    """Шаги участника: просроченные, на ближайшую неделю, отмеченные им самим."""
    today = today or date.today()
    week = (today + timedelta(days=7)).isoformat()
    rows = conn.execute(
        "SELECT s.*, g.title AS goal FROM decl_steps s JOIN decl_goals g ON g.id=s.goal_id "
        "WHERE s.person_id=? ORDER BY s.deadline IS NULL, s.deadline, s.id", (pid,)).fetchall()
    late, soon, self_marked, undated = [], [], [], []
    for s in rows:
        dl = (s["deadline"] or "")[:10]
        if s["done"]:
            if s["self_marked"]:
                self_marked.append(s)
            continue
        if dl and dl < today.isoformat():
            late.append(s)
        elif dl and dl <= week:
            soon.append(s)
        elif not dl:
            undated.append(s)
    return {"late": late, "soon": soon, "self_marked": self_marked, "undated": undated,
            "total": len(rows), "done": sum(1 for s in rows if s["done"])}


def note(conn: sqlite3.Connection, card: int) -> dict | None:
    n = call_digest.last_note(conn, card)
    if not n:
        return None
    return {**dict(n), "promises": _jl(n["promises"]), "check": _jl(n["promises_check"])}


def member(conn: sqlite3.Connection, p: sqlite3.Row, reports_stat: dict | None = None) -> dict:
    """Строка экрана команды по одному участнику."""
    from .roster import card_id
    card = card_id(p["id"])
    n = note(conn, card)
    return {"card": card, "pid": p["id"], "name": p["name"], "progress": round((p["progress"] or 0) * 100),
            "steps": steps(conn, p["id"]), "note": n, "state": n["state"] if n else None,
            "rep": reports_stat, "native": p["source"] == "платформа"}


def team(conn: sqlite3.Connection, flow: sqlite3.Row, team_title: str, people: list[sqlite3.Row]) -> dict:
    """Экран команды для ментора: участники по тревожности, общий абзац со созвона."""
    from . import reports
    stats = {}
    try:
        h = reports.history(conn, flow)
        stats = h["stats"] if h["has_chat"] else {}
    except Exception:
        pass
    from .roster import card_id
    rows = [member(conn, p, stats.get(card_id(p["id"]))) for p in people]
    rows.sort(key=lambda m: (STATE_ORDER.get(m["state"], 3), m["progress"], m["name"]))
    tn = call_digest.last_team_note(conn, flow["name"], team_title)
    avg = round(sum(m["progress"] for m in rows) / len(rows)) if rows else 0
    return {"members": rows, "team_note": tn, "avg": avg,
            "counts": {s: sum(1 for m in rows if m["state"] == s) for s in ("идёт", "буксует", "выпадает")},
            "to_confirm": sum(len(m["steps"]["self_marked"]) for m in rows)}


def own(conn: sqlite3.Connection, person: sqlite3.Row) -> dict:
    """Трекер участника: только своё и только бережная заметка."""
    from .roster import card_id
    card = card_id(person["id"])
    n = note(conn, card)
    team_avg = None
    if person["team_id"]:
        r = conn.execute("SELECT AVG(progress) FROM people WHERE team_id=? AND status='в потоке'",
                         (person["team_id"],)).fetchone()
        team_avg = round((r[0] or 0) * 100)
    return {"progress": round((person["progress"] or 0) * 100), "team_avg": team_avg,
            "steps": steps(conn, person["id"]),
            "note": {"when": n["call_when"], "title": n["call_title"], "text": n["member_note"],
                     "promises": n["promises"], "check": n["check"]} if n and (n["member_note"] or n["promises"]) else None,
            "native": person["source"] == "платформа"}


def self_mark(conn: sqlite3.Connection, pid: int, sid: int, who: str) -> bool:
    """Участник отмечает свой шаг (или снимает свою же отметку)."""
    from . import decl
    s = conn.execute("SELECT * FROM decl_steps WHERE id=? AND person_id=?", (sid, pid)).fetchone()
    if not s:
        raise decl.DeclError("Шаг не из вашей декларации.")
    if s["done"] and not s["self_marked"]:
        raise decl.DeclError("Этот шаг подтвердил ментор — снять отметку может он.")
    done = not s["done"]
    conn.execute("UPDATE decl_steps SET done=?, done_by=?, done_at=datetime('now'), self_marked=? WHERE id=?",
                 (int(done), f"{who} (сам)" if done else None, int(done), sid))
    decl.recount(conn, pid)
    return done


def confirm(conn: sqlite3.Connection, pid: int, sid: int, who: str) -> None:
    conn.execute("UPDATE decl_steps SET self_marked=0, done_by=COALESCE(done_by,'') || ? "
                 "WHERE id=? AND person_id=? AND self_marked=1", (f" · подтвердил {who}", sid, pid))
