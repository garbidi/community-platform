"""Отбор в основной ММ — кто из участников 4-дневного мастермайнда идёт дальше.

Решение Станислава 02.11.2026: один поток (ММ8), без отдельного потока «4 дня»
и без кабинета на каждого. В поток сразу попадают все из зала — таблица Лилии
(fourday.sync по тем же правилам, что hall.py). На доске организатор раскладывает
людей: не решено → думает → идёт дальше / не идёт. Условие «идёт дальше»
(оплата или слово) пока не определено — это просто решение организатора.

Хранится в people.next_flow (да | думает | нет | NULL) + кто и когда (next_by, next_at).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any


# колонки доски: ключ → (заголовок, значение next_flow)
LANES = [
    ("open", "Не решено", None),
    ("maybe", "Думает", "думает"),
    ("yes", "Идёт дальше", "да"),
    ("no", "Не идёт", "нет"),
]
BY_KEY = {k: v for k, _, v in LANES}


def roster_flows(conn) -> list[sqlite3.Row]:
    """Потоки, у которых участники 4 дней читаются из таблицы (сейчас — ММ8)."""
    return conn.execute(
        "SELECT f.* FROM flows f JOIN settings s ON s.key='fourday:sheet:' || f.id "
        "WHERE s.value<>'' AND f.status<>'завершён' ORDER BY f.id DESC").fetchall()


def _short(s: str, n: int = 60) -> str:
    s = (s or "").strip().split("\n")[0]
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def board(conn, f) -> dict[str, Any]:
    from . import fourday
    from .roster import card_id
    people = []
    for p in conn.execute("SELECT * FROM people WHERE flow=? AND status='в потоке' ORDER BY name", (f["name"],)):
        try:
            prof = json.loads(p["profile"] or "{}")
        except ValueError:
            prof = {}
        lane = next((k for k, _, v in LANES if v == p["next_flow"]), "open")
        review = conn.execute("SELECT status FROM reviews WHERE tg_user_id=? ORDER BY id DESC LIMIT 1",
                              (card_id(p["id"]),)).fetchone()
        people.append({
            "id": p["id"], "card": card_id(p["id"]), "name": p["name"], "lane": lane,
            "photo": fourday.photo_path(p["id"]).exists(),
            "city": prof.get("city", ""), "niche": _short(prof.get("niche", "")),
            "inviter": prof.get("inviter", ""), "mentor": prof.get("mentor", ""),
            "knows": prof.get("knows_mm", ""), "risk": prof.get("risk", ""),
            "revenue": prof.get("revenue", ""), "profit": prof.get("profit", ""),
            "motives": prof.get("motives", ""), "point_a": prof.get("point_a", ""), "point_b": prof.get("point_b", ""),
            "expect": prof.get("expect", ""), "comment": prof.get("comment", ""), "mentor_note": prof.get("mentor_note", ""),
            "zoom": prof.get("zoom", ""), "username": p["username"] or "", "phone": p["phone"] or "",
            "review": review["status"] if review else None,
            "at": p["next_at"] or "", "by": p["next_by"] or "",
        })
    counts = {k: sum(1 for x in people if x["lane"] == k) for k, _, _ in LANES}
    sync = conn.execute("SELECT * FROM flow_sync WHERE flow=?", (f["name"],)).fetchone()
    cities = sorted({x["city"] for x in people if x["city"]}, key=lambda c: (-sum(1 for x in people if x["city"] == c), c))
    mentors = sorted({x["mentor"] for x in people if x["mentor"]},
                     key=lambda m: (m.isdigit(), int(m) if m.isdigit() else 0, m))   # имена, потом группы по номеру
    return {"flow": f, "people": people, "lanes": LANES, "counts": counts, "total": len(people),
            "sync": dict(sync) if sync else None, "cities": cities, "mentors": mentors}


def summary(conn, f) -> dict[str, Any]:
    """Итоги отбора для страницы потока: сколько в каждой колонке."""
    rows = conn.execute("SELECT next_flow, COUNT(*) n FROM people WHERE flow=? AND status='в потоке' GROUP BY next_flow",
                        (f["name"],)).fetchall()
    by = {r["next_flow"]: r["n"] for r in rows}
    counts = {k: by.get(v, 0) for k, _, v in LANES}
    return {"counts": counts, "total": sum(counts.values()), "lanes": LANES}


def decide(conn, flow_name: str, pid: int, lane: str, who: str) -> dict[str, Any]:
    from . import fourday
    if lane not in BY_KEY:
        raise ValueError("Непонятная колонка.")
    p = conn.execute("SELECT * FROM people WHERE id=? AND flow=?", (pid, flow_name)).fetchone()
    if not p:
        raise ValueError("Участник не найден.")
    fourday.set_next(conn, pid, BY_KEY[lane] or "", who)
    return {"ok": True, "lane": lane}
