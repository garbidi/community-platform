"""Пути и настройки. Меняются переменными окружения, значения по умолчанию —
под текущую машину."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# База знаний сообщества: её собирает vershina-kb, приложение только читает.
KB_DB = Path(os.environ.get(
    "VERSHINA_KB_DB", "/opt/vershina/vershina-kb-data/kb.sqlite"))

# Исходники сборщика: оттуда берутся формулы поведенческих метрик,
# чтобы цифры в приложении и в досье не разъезжались.
KB_SRC = Path(os.environ.get("VERSHINA_KB_SRC", ROOT.parent / "vershina-kb"))

# Своя база приложения: пользователи, роли, привязки, журнал доступа,
# поисковый индекс. База знаний при этом остаётся неизменной.
APP_DB = Path(os.environ.get(
    "VERSHINA_APP_DB", "/opt/vershina/vershina-app-data/app.sqlite"))

# Расшифровки созвонов — обычные txt рядом с записями.
CALLS_DIR = Path(os.environ.get(
    "VERSHINA_CALLS_DIR",
    "/opt/vershina/src/Записи созвонов"))

SESSION_COOKIE = "vershina_session"
SESSION_DAYS = 14

# Сколько высказываний показывать на странице карточки.
PAGE = 50

# Фото профиля участников: их скачивает сборщик чатов, когда фото открыто.
AVATAR_DIR = Path(os.environ.get("VERSHINA_AVATAR_DIR", KB_DB.parent / "media" / "avatars"))
