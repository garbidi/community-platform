"""Разграничение доступа. Ограничение стоит на уровне данных: роль определяет,
чьи записи вообще попадут в выборку, а не какие кнопки спрятаны в интерфейсе.

Матрица — из плана, раздел «Кто что видит».
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import store

ALL = None  # «видит всех» — отсутствие фильтра по участникам


def is_participant(role: str) -> bool:
    return role in ("participant", "participant_plus")


def sees_all_profiles(role: str) -> bool:
    return role in ("coordinator", "admin")


def sees_messages(role: str) -> bool:
    """Кто видит сам архив переписки.

    Участнику он не показывается: человек приходил в чат разговаривать,
    а не в базу данных, и наличие архива — не то, что он должен узнавать
    из интерфейса. Согласие на обработку переписки — отдельный разговор
    с сообществом, см. план, раздел 07.
    """
    return not is_participant(role)


def edits_results(role: str) -> bool:
    """Кто вносит вовлечения, штрафы и состав групп руками."""
    return role in ("coordinator", "admin")


def leads_people(role: str) -> bool:
    """Разборы по записям и цели участника: ведёт тот, кто работает с человеком.
    Ментор — только по своим (это отсекает may_open), деньги ему не положены."""
    return role in ("mentor", "coordinator", "admin")


def sees_calls(role: str) -> bool:
    """Расшифровки созвонов — рабочий материал команды, участникам не видны."""
    return role in ("mentor", "coordinator", "admin")


def sees_flow(role: str) -> bool:
    return role in ("coordinator", "admin")


def manages_flows(role: str) -> bool:
    """Создавать потоки, команды, назначать менторов — без разработчика."""
    return role in ("coordinator", "admin")


def manages_telegram(role: str) -> bool:
    """Вход в телеграм-аккаунт сообщества — у куратора он под рукой."""
    return role in ("coordinator", "admin")


def manages_people(role: str) -> bool:
    """«Доступы»: координатор — менторы и трекеры, администратор — все."""
    return role in ("coordinator", "admin")


def sees_finance(role: str) -> bool:
    return role == "admin"


def is_admin(role: str) -> bool:
    return role == "admin"


def extended(role: str) -> bool:
    return role in ("participant_plus", "coordinator", "admin")


def visible_ids(conn: sqlite3.Connection, user: sqlite3.Row) -> list[int] | None:
    """Чьи высказывания и профили доступны этому пользователю.
    None — ограничений нет."""
    role = user["role"]
    if sees_all_profiles(role):
        return ALL
    if role == "mentor":
        return store.mentor_participants(conn, user["id"])
    return [user["tg_user_id"]] if user["tg_user_id"] else []


def may_open(conn: sqlite3.Connection, user: sqlite3.Row, tg_id: int) -> bool:
    allowed = visible_ids(conn, user)
    return allowed is ALL or tg_id in allowed


def nav(user: sqlite3.Row) -> list[dict[str, str]]:
    role = user["role"]
    items = [{"href": "/", "title": "Обзор"}]
    if is_participant(role):
        items.append({"href": "/me", "title": "Мой профиль"})
    else:
        items.append({"href": "/participants", "title": "Участники"})
    # «Поиск» в меню вернётся, когда будет по чему искать в текущем потоке
    # (чаты ММ7, созвоны). Пока поиск по архиву — во вкладке «Архив» участников.
    if sees_flow(role):
        items.append({"href": "/flow", "title": "Динамика"})
    if is_participant(role):
        items.append({"href": "/my-team", "title": "Моя группа"})
    else:
        if role in ("mentor", "coordinator"):
            items.append({"href": "/teams/mine", "title": "Моя команда"})
        items.append({"href": "/teams", "title": "Команды"})
    if sees_calls(role):
        items.append({"href": "/calls", "title": "Созвоны"})
    if extended(role) and is_participant(role):
        items.append({"href": "/extended", "title": "Тариф"})
    # служебное — в свёрнутом блоке «Управление» внизу меню (base.html)
    adm = {"group": "Управление"}
    if manages_flows(role):
        items.append({"href": "/flows", "title": "Потоки", **adm})
        items.append({"href": "/rows", "title": "Таблица потока", **adm})
    if manages_flows(role):
        items.append({"href": "/posts", "title": "Посты", **adm})
        items.append({"href": "/sensations", "title": "Сенсационные", **adm})
    if manages_telegram(role):
        items.append({"href": "/telegram", "title": "Телеграм", **adm})
    if manages_people(role):
        items.append({"href": "/admin/users", "title": "Доступы", **adm})
    if is_admin(role):
        items.append({"href": "/admin/log", "title": "Журнал", **adm})
        items.append({"href": "/admin/finance", "title": "Финансы", **adm})
    return items


def hidden_note(role: str) -> str | None:
    """Что роль сознательно не видит — показывается на месте раздела,
    чтобы разграничение было явным, а не выглядело поломкой."""
    if role == "coordinator":
        return "Раздел «Финансы» скрыт для вашей роли."
    if role == "mentor":
        return "Вам видны участники ваших команд. Деньги участников не отображаются."
    if is_participant(role):
        return "Вам видно только ваше участие в потоке."
    return None
