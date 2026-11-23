#!/usr/bin/env python3
"""Поставить долгую работу в очередь и сразу вернуться.

Этим помощник отвечает на «сделай большой разбор» или «запусти фоном»:
он кладёт задачу сюда, тут же говорит собеседнику, что взялся, и
заканчивает ход. Работу выполнит бот отдельным ходом и пришлёт результат
в чат сам.

    job.py add "Разобрать чаты ММ5 и ММ6 и собрать стратегию вовлечения"
    job.py list

Задачу описывать подробно и самодостаточно: выполняться она будет
с чистого листа, без памяти текущего разговора.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import jobs  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("add", "list"):
        print(__doc__)
        return 1

    chat = os.environ.get("VERSHINA_CHAT_ID", "").strip()
    if not chat:
        print("Не знаю, в какой чат слать результат "
              "(нет VERSHINA_CHAT_ID). Задача не поставлена.")
        return 1

    if sys.argv[1] == "list":
        rows = jobs.waiting(chat)
        if not rows:
            print("Ничего не выполняется.")
            return 0
        for r in rows:
            print(f"  #{r['id']} [{r['state']}] {r['task'][:70]}")
        return 0

    task = " ".join(sys.argv[2:]).strip()
    if len(task) < 15:
        print("Опиши задачу подробнее: выполняться она будет с чистого "
              "листа, без памяти этого разговора.")
        return 1

    job_id = jobs.add(chat, task)
    print(f"Задача #{job_id} поставлена в очередь. Результат придёт в чат "
          "отдельным сообщением. Скажи об этом собеседнику и заканчивай ход — "
          "ждать здесь не нужно.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
