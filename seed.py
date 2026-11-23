#!/usr/bin/env python3
"""Первый запуск: создать базу приложения, собрать поисковый индекс
и завести учётные записи — по одной на каждую роль.

    python3 seed.py                 создать недостающее, пароли выдать случайные
    python3 seed.py --password 1234 задать всем один пароль (только для локальной проверки)
    python3 seed.py --reindex       только перестроить поисковый индекс
    python3 seed.py --reset         удалить пользователей и создать заново

Пароли печатаются один раз — в базе лежат только их хэши.
"""

from __future__ import annotations

import argparse
import secrets
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

from app import corpus, store  # noqa: E402

# Администратора заводит мастер настройки: его имя и пароль задаёт сам
# хозяин, поэтому здесь остаются только служебные учётки для проверки.
DEMO = [
    ("admin", "Администратор", "admin", None),
    ("coordinator", "Координатор потока", "coordinator", None),
    ("mentor", "Ментор потока", "mentor", None),
    ("participant", None, "participant", "link"),
    ("participant_plus", None, "participant_plus", "link"),
]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--password", help="один пароль на все учётки (для проверки)")
    p.add_argument("--reindex", action="store_true", help="только индекс")
    p.add_argument("--reset", action="store_true", help="пересоздать пользователей")
    args = p.parse_args()

    store.init()
    conn = store.connect()

    if args.reset:
        conn.executescript("DELETE FROM sessions; DELETE FROM assignments; "
                           "DELETE FROM users;")
        print("Пользователи удалены.")

    print("Индексирую корпус…")
    n = corpus.reindex(conn)
    print(f"  в индексе высказываний: {n}")
    if args.reindex:
        conn.commit()
        return 0

    people = corpus.participants()
    if not people:
        print("В базе знаний нет участников — сначала соберите её через vershina-kb.")
        return 1

    # Участников для демонстрационных учёток берём из середины списка: у самых
    # активных слишком много сообщений, страница грузится дольше, чем нужно
    # для проверки.
    pool = people[len(people) // 3:]
    created = []

    for login, name, role, link in DEMO:
        if store.user_by_login(conn, login):
            continue
        tg = None
        if link == "link" and pool:
            person = pool.pop(0)
            tg, name = person["user_id"], person["name"]
        password = args.password or secrets.token_urlsafe(9)
        store.create_user(conn, login, name or login, role, password, tg)
        created.append((login, store.ROLES[role], password,
                        f"{name} (tg {tg})" if tg else "—"))

    mentor = store.user_by_login(conn, "mentor")
    if mentor and not store.mentor_participants(conn, mentor["id"]):
        assigned = [p["user_id"] for p in people[:6]]
        store.set_assignments(conn, mentor["id"], assigned)
        print(f"\nМентору закреплены участники: "
              + ", ".join(corpus.name_of(t) for t in assigned))

    conn.commit()

    if created:
        print("\nУчётные записи (пароль показывается только сейчас):\n")
        width = max(len(c[1]) for c in created)
        for login, role, password, who in created:
            print(f"  {role:<{width}}  логин {login:<18} пароль {password:<14} {who}")
    else:
        print("\nВсе учётные записи уже существуют. Пароль можно сменить "
              "в разделе «Доступы» или через --reset.")

    print("\nЗапуск: ./run.sh  →  http://127.0.0.1:8000")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
