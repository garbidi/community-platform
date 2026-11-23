"""«Доступы»: кто работает в платформе — организаторы, менторы, трекеры.

Кого видит ментор, решают команды в «Потоках» (ментор и трекер команды
видят её участников), поэтому ручных закреплений здесь больше нет.
Пароли придумывает платформа и показывает один раз — готовым сообщением,
которое можно переслать человеку; в базе остаётся только хэш.

Координатор управляет менторами и трекерами; роли, администраторов
и координаторов меняет только администратор.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
from typing import Any

from . import store

STAFF_ROLES = {"mentor": "Ментор / трекер", "coordinator": "Координатор", "admin": "Администратор"}
GROUPS = (("Организаторы", ("admin", "coordinator")), ("Менторы и трекеры", ("mentor",)))


class StaffError(ValueError):
    """Ошибка, которую можно показать человеку как есть."""


def new_password() -> str:
    # без похожих символов: пароль часто диктуют или перепечатывают
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(12))


def may_manage(actor: sqlite3.Row, target_role: str) -> bool:
    if actor["role"] == "admin":
        return True
    return actor["role"] == "coordinator" and target_role == "mentor"


def listing(conn: sqlite3.Connection) -> dict[str, Any]:
    # «был(а)» — последнее действие в платформе: вход запоминается в браузере,
    # и по одному вводу пароля активный человек выглядел бы давно не заходившим
    seen = {r["user_id"]: r["at"] for r in conn.execute(
        "SELECT user_id, MAX(at) AS at FROM access_log WHERE user_id IS NOT NULL GROUP BY user_id")}
    logged = {r[0] for r in conn.execute("SELECT DISTINCT user_id FROM access_log WHERE action='вход'")}
    teams: dict[int, list[dict]] = {}
    for r in conn.execute(
            "SELECT s.user_id, s.role, t.title, t.id AS tid, f.name AS flow, f.status FROM flow_staff s "
            "JOIN flow_teams t ON t.id=s.flow_team_id JOIN flows f ON f.id=t.flow_id "
            "ORDER BY f.start DESC, t.no"):
        teams.setdefault(r["user_id"], []).append({
            "team": r["title"].split("—")[0].strip(), "tid": r["tid"], "flow": r["flow"],
            "role": r["role"], "done": r["status"] == "завершён"})
    sees = {r["mentor_id"]: r["n"] for r in conn.execute(
        "SELECT mentor_id, COUNT(*) n FROM assignments GROUP BY mentor_id")}
    people = []
    for u in conn.execute("SELECT * FROM users ORDER BY name"):
        people.append({**dict(u), "last": seen.get(u["id"]) if u["id"] in logged else None,
                       "teams": teams.get(u["id"], []),
                       "sees": sees.get(u["id"], 0)})
    active = [p for p in people if p["active"] and p["role"] in STAFF_ROLES]
    groups = [{"title": t, "people": [p for p in active if p["role"] in roles]} for t, roles in GROUPS]
    return {
        "groups": groups,
        "off": [p for p in people if not p["active"] or p["role"] not in STAFF_ROLES],
        "never": [p for p in active if not p["last"] and p["role"] == "mentor"],
    }


def _clean(name: str, login: str) -> tuple[str, str]:
    name = re.sub(r"\s+", " ", name or "").strip()
    login = (login or "").strip().lstrip("@").lower()
    if not name:
        raise StaffError("Укажите имя и фамилию.")
    if not re.fullmatch(r"[a-z0-9_.]{3,40}", login):
        raise StaffError("Логин — ник в телеграме: латиница, цифры, «_» и «.», от 3 знаков.")
    return name, login


def create(conn: sqlite3.Connection, actor: sqlite3.Row, name: str, login: str, role: str) -> dict:
    name, login = _clean(name, login)
    if role not in STAFF_ROLES or not may_manage(actor, role):
        raise StaffError("Такую роль может выдать только администратор.")
    password = new_password()
    try:
        store.create_user(conn, login, name, role, password)
    except sqlite3.IntegrityError:
        raise StaffError(f"Логин «{login}» уже занят.")
    return {"name": name, "login": login, "password": password}


def _target(conn: sqlite3.Connection, actor: sqlite3.Row, uid: int) -> sqlite3.Row:
    u = store.user_by_id(conn, uid)
    if not u:
        raise StaffError("Пользователь не найден.")
    if not may_manage(actor, u["role"]):
        raise StaffError("Менять организаторов может только администратор.")
    return u


def update(conn: sqlite3.Connection, actor: sqlite3.Row, uid: int, name: str, login: str, role: str) -> None:
    u = _target(conn, actor, uid)
    name, login = _clean(name, login)
    role = role or u["role"]
    if role != u["role"]:
        if actor["role"] != "admin":
            raise StaffError("Роль меняет только администратор.")
        if role not in STAFF_ROLES:
            raise StaffError("Неизвестная роль.")
        if u["id"] == actor["id"]:
            raise StaffError("Свою роль поменять нельзя — попросите другого администратора.")
    try:
        conn.execute("UPDATE users SET name=?, login=?, role=? WHERE id=?", (name, login, role, uid))
    except sqlite3.IntegrityError:
        raise StaffError(f"Логин «{login}» уже занят.")


def reset_password(conn: sqlite3.Connection, actor: sqlite3.Row, uid: int) -> dict:
    u = _target(conn, actor, uid)
    password = new_password()
    store.set_password(conn, uid, password)
    # старый пароль больше не годится — и открытые входы закрываем
    conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    return {"name": u["name"], "login": u["login"], "password": password}


def set_active(conn: sqlite3.Connection, actor: sqlite3.Row, uid: int, active: bool) -> None:
    u = _target(conn, actor, uid)
    if u["id"] == actor["id"] and not active:
        raise StaffError("Себя отключить нельзя.")
    conn.execute("UPDATE users SET active=? WHERE id=?", (1 if active else 0, uid))
    if not active:
        conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
