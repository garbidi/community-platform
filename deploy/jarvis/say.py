#!/usr/bin/env python3
"""Сказать что-то в чат немедленно, не дожидаясь конца работы.

Обычный ответ уходит в телеграм, только когда вся работа закончена. Если
работа долгая — зайти в зум, дождаться события, что-то собрать, — сначала
отправь готовую часть этим, а потом занимайся остальным.

    python3 say.py "Ссылка: https://... Захожу в комнату, запись включу."

Чат берётся из переменной VERSHINA_CHAT_ID, токен — из bot.env приложения.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

ENV = Path(os.environ.get(
    "VERSHINA_BOT_ENV",
    "/data/app/bot.env"))


def token() -> str:
    got = os.environ.get("VERSHINA_BOT_TOKEN", "")
    if got:
        return got
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if line.startswith("VERSHINA_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("нет токена бота")


def main() -> int:
    if len(sys.argv) < 2:
        print("использование: say.py «текст» [chat_id]", file=sys.stderr)
        return 2
    text = sys.argv[1]
    chat = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("VERSHINA_CHAT_ID", "")
    if not chat:
        print("неизвестно, в какой чат писать", file=sys.stderr)
        return 2

    payload = json.dumps({"chat_id": int(chat), "text": text,
                          "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token()}/sendMessage",
        data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            ok = json.load(resp).get("ok")
    except Exception as exc:                    # сеть, чат, токен
        print(f"не отправилось: {exc}", file=sys.stderr)
        return 1
    print("отправлено" if ok else "телеграм не принял")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
