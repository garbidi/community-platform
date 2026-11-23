"""Вовлечения, деньги и группы потока.

Вовлечение здесь — факт: кто вовлёк, когда, на какую сумму и в каком статусе.
Сейчас записи заносятся руками, поэтому у каждой есть автор и время внесения.
Когда появится источник, который видит приход денег, он будет писать в ту же
таблицу и проставлять себя в поле origin — ни интерфейс, ни расчёты
от этого не меняются.

Деньги считаются раздельно: принесённое вовлечениями и списания (штрафы,
возвраты, бонусы). Итог — их сумма, поэтому по человеку сразу видно,
вышел он в плюс или в минус.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any

STATUSES = ("оплачено", "предоплата", "ожидание")
KINDS = ("штраф", "возврат", "бонус", "прочее")
MEMBER_ROLES = ("участник", "лидер", "ментор")


# ----------------------------------------------------------------- вовлечения

def add_involvement(conn: sqlite3.Connection, tg_id: int, happened: str,
                    amount: int, who: str | None, status: str, note: str | None,
                    added_by: str, origin: str = "вручную") -> int:
    if status not in STATUSES:
        status = "оплачено"
    cur = conn.execute(
        "INSERT INTO involvements (tg_user_id, happened, amount, who, status, note, "
        "origin, added_by) VALUES (?,?,?,?,?,?,?,?)",
        (tg_id, happened or date.today().isoformat(), int(amount), who or None,
         status, note or None, origin, added_by))
    return int(cur.lastrowid)


def add_charge(conn: sqlite3.Connection, tg_id: int, happened: str, amount: int,
               kind: str, reason: str | None, added_by: str) -> int:
    if kind not in KINDS:
        kind = "прочее"
    # Штраф — это минус, даже если его ввели без знака: так ошибиться труднее.
    amount = -abs(int(amount)) if kind == "штраф" else int(amount)
    cur = conn.execute(
        "INSERT INTO charges (tg_user_id, happened, amount, kind, reason, added_by) "
        "VALUES (?,?,?,?,?,?)", (tg_id, happened or date.today().isoformat(),
                                 amount, kind, reason or None, added_by))
    return int(cur.lastrowid)


def drop_involvement(conn: sqlite3.Connection, row_id: int) -> None:
    conn.execute("DELETE FROM involvements WHERE id=?", (row_id,))


def drop_charge(conn: sqlite3.Connection, row_id: int) -> None:
    conn.execute("DELETE FROM charges WHERE id=?", (row_id,))


def involvements_of(conn: sqlite3.Connection, tg_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM involvements WHERE tg_user_id=? ORDER BY happened DESC, id DESC",
        (tg_id,)).fetchall()


def charges_of(conn: sqlite3.Connection, tg_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM charges WHERE tg_user_id=? ORDER BY happened DESC, id DESC",
        (tg_id,)).fetchall()


def totals_of(conn: sqlite3.Connection, tg_id: int) -> dict[str, int]:
    inv = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(amount),0) s, "
        "COALESCE(SUM(CASE WHEN status='оплачено' THEN amount ELSE 0 END),0) paid "
        "FROM involvements WHERE tg_user_id=?", (tg_id,)).fetchone()
    ch = conn.execute(
        "SELECT COALESCE(SUM(amount),0) s, "
        "COALESCE(SUM(CASE WHEN kind='штраф' THEN -amount ELSE 0 END),0) fines "
        "FROM charges WHERE tg_user_id=?", (tg_id,)).fetchone()
    return {"count": inv["n"], "brought": inv["s"], "paid": inv["paid"],
            "charges": ch["s"], "fines": ch["fines"],
            "net": inv["s"] + ch["s"]}


def totals_for(conn: sqlite3.Connection, tg_ids: list[int]) -> dict[int, dict[str, int]]:
    """Итоги сразу по списку людей — чтобы не бегать в базу на каждой строке."""
    if not tg_ids:
        return {}
    marks = ",".join("?" * len(tg_ids))
    out = {t: {"count": 0, "brought": 0, "paid": 0, "charges": 0, "fines": 0, "net": 0}
           for t in tg_ids}
    for r in conn.execute(
            f"SELECT tg_user_id, COUNT(*) n, COALESCE(SUM(amount),0) s, "
            f"COALESCE(SUM(CASE WHEN status='оплачено' THEN amount ELSE 0 END),0) paid "
            f"FROM involvements WHERE tg_user_id IN ({marks}) GROUP BY tg_user_id", tg_ids):
        out[r["tg_user_id"]].update(count=r["n"], brought=r["s"], paid=r["paid"])
    for r in conn.execute(
            f"SELECT tg_user_id, COALESCE(SUM(amount),0) s, "
            f"COALESCE(SUM(CASE WHEN kind='штраф' THEN -amount ELSE 0 END),0) fines "
            f"FROM charges WHERE tg_user_id IN ({marks}) GROUP BY tg_user_id", tg_ids):
        out[r["tg_user_id"]].update(charges=r["s"], fines=r["fines"])
    for t in out:
        out[t]["net"] = out[t]["brought"] + out[t]["charges"]
    return out


# ----------------------------------------------------------------- группы

def create_team(conn: sqlite3.Connection, title: str, flow: str, goal: int,
                note: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO teams (title, flow, goal, note) VALUES (?,?,?,?)",
        (title.strip(), flow.strip() or None, max(0, int(goal)), note or None))
    return int(cur.lastrowid)


def update_team(conn: sqlite3.Connection, team_id: int, title: str, flow: str,
                goal: int, note: str | None) -> None:
    conn.execute("UPDATE teams SET title=?, flow=?, goal=?, note=? WHERE id=?",
                 (title.strip(), flow.strip() or None, max(0, int(goal)),
                  note or None, team_id))


def archive_team(conn: sqlite3.Connection, team_id: int, archived: bool) -> None:
    conn.execute("UPDATE teams SET archived=? WHERE id=?", (1 if archived else 0, team_id))


def add_member(conn: sqlite3.Connection, team_id: int, tg_id: int, role: str) -> None:
    conn.execute("INSERT OR REPLACE INTO team_members (team_id, tg_user_id, role) "
                 "VALUES (?,?,?)", (team_id, tg_id, role if role in MEMBER_ROLES else "участник"))


def drop_member(conn: sqlite3.Connection, team_id: int, tg_id: int) -> None:
    conn.execute("DELETE FROM team_members WHERE team_id=? AND tg_user_id=?",
                 (team_id, tg_id))


def team(conn: sqlite3.Connection, team_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM teams WHERE id=?", (team_id,)).fetchone()


def members(conn: sqlite3.Connection, team_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM team_members WHERE team_id=? ORDER BY role='участник', tg_user_id",
        (team_id,)).fetchall()


def announcements(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Вовлечения, собранные по объявлениям в чатах ММ5/ММ6 — архив «Команд»:
    по человеку, сколько раз объявляли и когда последний."""
    from . import corpus
    out = []
    for r in conn.execute(
            "SELECT tg_user_id, COUNT(*) n, MIN(happened) first, MAX(happened) last "
            "FROM involvements GROUP BY tg_user_id ORDER BY n DESC, last DESC"):
        out.append({"tg": r["tg_user_id"], "name": corpus.name_of(r["tg_user_id"]),
                    "n": r["n"], "first": r["first"], "last": r["last"]})
    return out


def team_of(conn: sqlite3.Connection, tg_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT t.* FROM teams t JOIN team_members m ON m.team_id = t.id "
        "WHERE m.tg_user_id=? AND t.archived=0 ORDER BY t.id DESC LIMIT 1",
        (tg_id,)).fetchone()


def teams(conn: sqlite3.Connection, with_archived: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM teams"
    if not with_archived:
        sql += " WHERE archived=0"
    return conn.execute(sql + " ORDER BY flow, title").fetchall()


def team_summary(conn: sqlite3.Connection, team_id: int) -> dict[str, Any]:
    row = team(conn, team_id)
    if row is None:
        return {}
    ids = [m["tg_user_id"] for m in members(conn, team_id)]
    per = totals_for(conn, ids)
    count = sum(p["count"] for p in per.values())
    brought = sum(p["brought"] for p in per.values())
    charges = sum(p["charges"] for p in per.values())
    fines = sum(p["fines"] for p in per.values())
    goal = row["goal"] or 0
    return {
        "team": row, "ids": ids, "per": per,
        "members": len(ids), "count": count, "brought": brought,
        "charges": charges, "fines": fines, "net": brought + charges,
        "goal": goal, "left": max(0, goal - count),
        "share": min(1.0, count / goal) if goal else None,
    }


def flow_totals(conn: sqlite3.Connection) -> dict[str, int]:
    """Итоги по всем, а не только по тем, кто состоит в группе."""
    inv = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(amount),0) s FROM involvements").fetchone()
    ch = conn.execute("SELECT COALESCE(SUM(amount),0) s, "
                      "COALESCE(SUM(CASE WHEN kind='штраф' THEN -amount ELSE 0 END),0) f "
                      "FROM charges").fetchone()
    people = conn.execute("SELECT COUNT(DISTINCT tg_user_id) n FROM involvements").fetchone()
    return {"count": inv["n"], "brought": inv["s"], "charges": ch["s"],
            "fines": ch["f"], "net": inv["s"] + ch["s"], "people": people["n"]}
