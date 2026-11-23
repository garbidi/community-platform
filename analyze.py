#!/usr/bin/env python3
"""Очередь разборов участников: то, чем сейчас работает движок «queue».

Приложение складывает сюда запросы, разбор делает Claude Code:

    python3 analyze.py list              что ждёт разбора
    python3 analyze.py show 3            промпт запроса №3 (его и читает модель)
    python3 analyze.py save 3 ответ.md   положить готовый разбор
    cat ответ.md | python3 analyze.py save 3
    python3 analyze.py run 3             отдать запрос установленному claude -p
    python3 analyze.py fail 3 "причина"  пометить, что не получилось

Когда появится своя нейронка, эта команда станет не нужна: вызов провайдера
уже стоит внутри приложения, в app/analysis.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import analysis, corpus, store  # noqa: E402


def cmd_list() -> int:
    conn = store.connect()
    rows = analysis.pending(conn)
    if not rows:
        print("Очередь пуста.")
        return 0
    print(f"Ждут разбора: {len(rows)}\n")
    for r in rows:
        print(f"  №{r['id']}  {corpus.name_of(r['tg_user_id'])}"
              f" (tg {r['tg_user_id']})  вид: {r['kind']}"
              f"  запросил {r['requested_login']}  {r['created_at']}")
        print(f"      высказываний {r['messages_used']}, знаков {r['material_chars']}")
        print(f"      промпт: {r['prompt_path']}")
    return 0


def cmd_show(request_id: int) -> int:
    conn = store.connect()
    row = analysis.get(conn, request_id)
    if row is None:
        print(f"Запроса №{request_id} нет.")
        return 1
    print(Path(row["prompt_path"]).read_text(encoding="utf-8"))
    return 0


def cmd_save(request_id: int, path: str | None) -> int:
    conn = store.connect()
    row = analysis.get(conn, request_id)
    if row is None:
        print(f"Запроса №{request_id} нет.")
        return 1
    text = (Path(path).read_text(encoding="utf-8") if path
            else sys.stdin.read())
    if not text.strip():
        print("Пустой текст — нечего сохранять.")
        return 1
    analysis.save(conn, request_id, text)
    conn.commit()
    print(f"Разбор №{request_id} сохранён: {corpus.name_of(row['tg_user_id'])}, "
          f"{len(text)} знаков. Виден в карточке участника.")
    return 0


def cmd_run(request_id: int) -> int:
    conn = store.connect()
    ok = analysis.run_cli(conn, request_id)
    conn.commit()
    row = analysis.get(conn, request_id)
    print("Готово." if ok else f"Не получилось: {row['error'] if row else 'запрос не найден'}")
    return 0 if ok else 1


def cmd_fail(request_id: int, reason: str) -> int:
    conn = store.connect()
    analysis.fail(conn, request_id, reason)
    conn.commit()
    print(f"Запрос №{request_id} помечен как неудавшийся.")
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd, *rest = argv
    try:
        if cmd == "list":
            return cmd_list()
        if cmd == "show":
            return cmd_show(int(rest[0]))
        if cmd == "save":
            return cmd_save(int(rest[0]), rest[1] if len(rest) > 1 else None)
        if cmd == "run":
            return cmd_run(int(rest[0]))
        if cmd == "fail":
            return cmd_fail(int(rest[0]), rest[1] if len(rest) > 1 else "не указано")
    except (IndexError, ValueError):
        print(__doc__)
        return 1
    print(f"Не знаю команды «{cmd}».")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
