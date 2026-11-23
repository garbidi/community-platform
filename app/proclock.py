"""Замки между процессами: платформа работает несколькими процессами uvicorn,
а файл сессии Телеграма, чтение таблиц и фоновые задачи должны быть в одном
экземпляре. Замок — flock на файле рядом с базой; ядро снимает его само,
если процесс упал."""

from __future__ import annotations

import fcntl
import os
import threading
from contextlib import contextmanager

from .config import APP_DB

DIR = APP_DB.parent / "locks"
_threads: dict[str, threading.RLock] = {}
_guard = threading.Lock()
_kept: list = []          # файлы «вечных» замков держим открытыми до конца процесса


def _path(name: str) -> str:
    DIR.mkdir(parents=True, exist_ok=True)
    return str(DIR / f"{name}.lock")


@contextmanager
def held(name: str):
    """Только один поток во всех процессах сразу (ждёт своей очереди)."""
    with _guard:
        lock = _threads.setdefault(name, threading.RLock())
    with lock:
        fd = os.open(_path(name), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def leader(name: str) -> bool:
    """Занять роль на всё время жизни процесса: True — у этого процесса."""
    fd = os.open(_path(name), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    _kept.append(fd)
    return True
