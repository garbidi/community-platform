"""Завести вход менторам и трекерам по списку «Имя @логин».

    python3 mentors_import.py [список]      по умолчанию — mentors.txt рядом с базой приложения

Логин в приложении — телеграм-логин без @. Участник в базе знаний ищется
по тому же логину: тогда у ментора в приложении открывается и его собственная
карточка. Повторный запуск не плодит дублей — обновляет имя и привязку.
Пароли новых учёток пишутся в mentor-passwords.txt рядом с базой (права 600):
раздавать лично, в чаты не пересылать.
"""

import os
import secrets
import sqlite3
import sys
from pathlib import Path

from app import store
from app.config import APP_DB, KB_DB


def parse(path: Path) -> list[tuple[str, str]]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "@" not in line:
            continue
        name, login = line.rsplit("@", 1)
        out.append((name.strip(), login.strip()))
    return out


def main() -> None:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else APP_DB.parent / "mentors.txt"
    kb = sqlite3.connect(f"file:{KB_DB}?mode=ro", uri=True)
    store.init()
    conn = store.connect()
    created = []
    for name, tg_login in parse(src):
        login = tg_login.lower()
        hit = kb.execute("SELECT id FROM users WHERE lower(username)=?", (login,)).fetchone()
        tg_id = hit[0] if hit else None
        row = conn.execute("SELECT id FROM users WHERE login=?", (login,)).fetchone()
        if row:
            conn.execute("UPDATE users SET name=?, tg_user_id=COALESCE(?, tg_user_id) WHERE id=?",
                         (name, tg_id, row["id"]))
            print(f"обновлён  {login:28} {name}")
            continue
        password = secrets.token_urlsafe(8)
        uid = store.create_user(conn, login, name, "mentor", password)
        if tg_id:
            conn.execute("UPDATE users SET tg_user_id=? WHERE id=?", (tg_id, uid))
        created.append((name, login, password))
        print(f"заведён   {login:28} {name}{'' if tg_id else '  (в чатах ММ5/ММ6 не найден)'}")

    if created:
        out = APP_DB.parent / "mentor-passwords.txt"
        with open(out, "a", encoding="utf-8") as f:
            for name, login, password in created:
                f.write(f"{name}\t{login}\t{password}\n")
        os.chmod(out, 0o600)
        print(f"\nпароли {len(created)} новых учёток: {out}")


if __name__ == "__main__":
    main()
