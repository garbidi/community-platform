"""Окружение для `claude -p`: вход в Claude общий со Стенографом.

Глеб входит в Claude один раз — кнопкой на странице Стенографа.
Стенограф кладёт годовой токен в свой data/claude.json; каталог подключён
сюда только на чтение (VERSHINA_CLAUDE_TOKEN_FILE). Если файла нет — берём
CLAUDE_CODE_OAUTH_TOKEN из своего .env, как раньше.

Каталог, а не сам файл: Стенограф сохраняет токен заменой файла, и
примонтированный по отдельности файл остался бы старым.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

TOKEN_FILE = Path(os.environ.get("VERSHINA_CLAUDE_TOKEN_FILE", "/stenograf/claude.json"))
NOT_LOGGED = ("Claude не подключён: войдите в Claude на странице Стенографа "
              "(кнопка «Claude» вверху) — выжимки здесь работают от того же входа.")


def token() -> str | None:
    try:
        t = json.loads(TOKEN_FILE.read_text()).get("token")
        if t:
            return t
    except (OSError, ValueError):
        pass
    return os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or None


def env() -> dict[str, str]:
    """Окружение без следов родительского Claude Code, с общим токеном."""
    e = {k: v for k, v in os.environ.items()
         if not k.startswith(("CLAUDE_CODE_", "CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT"))}
    t = token()
    if t:
        e["CLAUDE_CODE_OAUTH_TOKEN"] = t
    return e


def explain(out: str) -> str:
    """Понятный текст вместо «Not logged in · Please run /login»."""
    low = (out or "").lower()
    if "not logged in" in low or "/login" in low or "oauth token has expired" in low \
            or "invalid bearer token" in low:
        return NOT_LOGGED
    return out
