"""«Команды»: соревнование команд потока по вовлечённым.

Каждая команда ставит себе цель — сколько человек вовлечь за поток — и идёт
к ней. Вовлечённые берутся из таблиц деклараций: блок «Планка по вовлечению»
у каждого участника (таблица involved). Цели и шаги деклараций здесь не
показываем — это личное участника, а не его команды.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta
from typing import Any

from . import flows

_DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})")
_PREFIX_RE = re.compile(r"^\s*вовлеч[её]нн?ый\s*\d*\s*[·:/\-–—]*\s*", re.I)


def inv_date(text: str, today: date | None = None) -> date | None:
    """Дата из строки планки: «Вовлечённый 2 · 27.09 большая программа»."""
    today = today or date.today()
    m = _DATE_RE.search(text or "")
    if not m:
        return None
    d, mth = int(m.group(1)), int(m.group(2))
    try:
        got = date(today.year, mth, d)
    except ValueError:
        return None
    return got if got <= today + timedelta(days=1) else date(today.year - 1, mth, d)


def inv_label(text: str) -> str:
    """«Вовлечённый 2 · 27.09 большая программа» → «27.09 большая программа»."""
    return _PREFIX_RE.sub("", text or "").strip(" ·") or "без подробностей"


def _short(title: str | None) -> str:
    return (title or "Без команды").split("—")[0].strip()


def _first_last(name: str) -> str:
    parts = (name or "").split()
    return f"{parts[1]} {parts[0]}" if len(parts) == 2 else name


def _team_people(conn, flow_name: str, title: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT p.* FROM people p JOIN teams t ON t.id=p.team_id "
        "WHERE p.flow=? AND t.flow=? AND t.title=? AND p.status='в потоке' ORDER BY p.name",
        (flow_name, flow_name, title)).fetchall()


def _involved(conn, pid: int, today: date) -> dict[str, Any]:
    rows = conn.execute("SELECT idx, text, done FROM involved WHERE person_id=? ORDER BY idx",
                        (pid,)).fetchall()
    done = [{"label": inv_label(r["text"]), "date": inv_date(r["text"], today),
             "over": "сверх" in (r["text"] or "").lower()} for r in rows if r["done"]]
    # строки «сверх личной планки» — перевыполнение, а не сама планка
    plan = sum(1 for r in rows if "сверх личной планки" not in (r["text"] or "").lower())
    return {"plan": plan, "done": done}


def my_team_ids(conn, user: sqlite3.Row) -> set[int]:
    """Команды потока, где пользователь ментор или трекер."""
    return {r[0] for r in conn.execute(
        "SELECT flow_team_id FROM flow_staff WHERE user_id=?", (user["id"],))}


def board(conn, flow: sqlite3.Row, today: date | None = None) -> dict[str, Any]:
    """Таблица команд потока: вовлечённые против своей цели, за неделю, лидер."""
    today = today or date.today()
    week_ago = today - timedelta(days=7)
    chats = {}
    for c in flows.chats(conn, flow["id"]):
        if c["kind"] == "команда" and c["team_id"]:
            chats.setdefault(c["team_id"], c)
    from .roster import dashboard_of
    dash = dashboard_of(conn, flow["name"])
    out = []
    for t in flows.teams(conn, flow["id"]):
        people = _team_people(conn, flow["name"], t["title"])
        done = plan = week = 0
        last = None
        for p in people:
            inv = _involved(conn, p["id"], today)
            plan += inv["plan"]
            done += len(inv["done"])
            for d in inv["done"]:
                if d["date"] and week_ago < d["date"] <= today:
                    week += 1
                if d["date"] and (last is None or d["date"] > last):
                    last = d["date"]
        # итог и цель команды — как в дашборде её таблицы (с выбывшими и долей
        # за выбывших потока); своя цель из «Потоков» важнее планки дашборда
        d = dash.get(t["title"])
        if d and d["done"] is not None:
            done = int(round(d["done"]))
            plan = int(round(d["plan"] or 0))
        goal = t.get("goal") or (plan if d and plan else None)
        out.append({
            "id": t["id"], "no": t["no"], "title": _short(t["title"]), "full": t["title"],
            "mentors": [m["name"] for m in t["mentors"]],
            "trackers": [m["name"] for m in t["trackers"]],
            "people": len(people), "done": done, "plan": plan, "week": week, "last": last,
            "goal": goal, "goal_from": "своя" if t.get("goal") else ("дашборд" if goal else None),
            "left": max(0, goal - done) if goal else None,
            "share": min(1.0, done / goal) if goal else None,
            # заливка плашки: путь к цели команды, а пока цели нет — к сумме личных планок
            "fill": min(1.0, done / goal) if goal else (min(1.0, done / plan) if plan else 0),
            "chat": chats.get(t["id"]),
        })
    # лидер сверху — по той же доле, что и заливка плашки: путь к своей цели
    # (пока целей нет — к сумме личных планок), при равенстве — по числу
    out.sort(key=lambda x: (-x["fill"], -x["done"], x["no"] or 0))
    # равный счёт — одно место на двоих
    place, prev = 0, None
    for i, x in enumerate(out, 1):
        key = (round(x["fill"], 4), x["done"])
        if key != prev:
            place, prev = i, key
        x["place"] = place if x["done"] else None
    goals = [x["goal"] for x in out if x["goal"]]
    return {
        "teams": out,
        "done": sum(x["done"] for x in out), "week": sum(x["week"] for x in out),
        "goal": sum(goals) if len(goals) == len(out) and out else None,
        "plan": sum(x["plan"] for x in out),
        "from": week_ago + timedelta(days=1), "to": today,
    }


def team(conn, flow_team_id: int, today: date | None = None) -> dict[str, Any] | None:
    """Страница команды: участники и их вовлечённые, лента по датам."""
    today = today or date.today()
    t = conn.execute("SELECT * FROM flow_teams WHERE id=?", (flow_team_id,)).fetchone()
    if not t:
        return None
    f = flows.get(conn, t["flow_id"])
    full = next(x for x in flows.teams(conn, f["id"]) if x["id"] == t["id"])
    from .roster import card_id
    members, feed = [], []
    for p in _team_people(conn, f["name"], t["title"]):
        inv = _involved(conn, p["id"], today)
        dates = [d["date"] for d in inv["done"] if d["date"]]
        name = _first_last(p["name"])
        members.append({"card": card_id(p["id"]), "name": name, "plan": inv["plan"],
                        "done": len(inv["done"]), "last": max(dates) if dates else None,
                        "inv": inv["done"]})
        for d in inv["done"]:
            # в ленте дата уже слева — из подписи её убираем
            label = re.sub(r"^\d{1,2}\.\d{1,2}\s*", "", d["label"]) or d["label"]
            feed.append({"date": d["date"], "name": name, "card": card_id(p["id"]),
                         "label": label, "over": d["over"]})
    members.sort(key=lambda m: (-m["done"], m["name"]))
    # строки дашборда, которых нет среди участников: выбывшие из команды и
    # доля за выбывших потока — без них сумма не сходится с таблицей
    from .roster import dashboard_of
    d = dashboard_of(conn, f["name"]).get(t["title"])
    extra = [r for r in (d["rows"] if d else []) if r.get("kind") != "участник" and (r.get("done") or r.get("plan"))]
    feed.sort(key=lambda x: (x["date"] is None, -(x["date"].toordinal() if x["date"] else 0)))
    done = sum(m["done"] for m in members)
    plan = sum(m["plan"] for m in members)
    if d and d["done"] is not None:
        done, plan = int(round(d["done"])), int(round(d["plan"] or 0))
    goal = t["goal"] or (plan if d and plan else None)
    chat = next((c for c in flows.chats(conn, f["id"]) if c["kind"] == "команда" and c["team_id"] == t["id"]), None)
    return {
        "flow": f, "id": t["id"], "title": _short(t["title"]), "full": t["title"], "sheet": t["sheet_id"],
        "mentors": [m["name"] for m in full["mentors"]], "trackers": [m["name"] for m in full["trackers"]],
        "staff_ids": {m["id"] for m in full["mentors"]} | {m["id"] for m in full["trackers"]},
        "members": members, "feed": feed, "done": done, "plan": plan, "extra": extra,
        "dash": bool(d), "goal": goal, "goal_from": "своя" if t["goal"] else ("дашборд" if goal else None), "left": max(0, goal - done) if goal else None,
        "share": min(1.0, done / goal) if goal else None, "chat": chat,
    }
