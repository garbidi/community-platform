"""Сторож календаря: раз в несколько минут проверяет Google Календарь и
шлёт в телеграм напоминание за час до события, а утром — сводку дня.

Запуск/остановка: ./gcal_watch.sh start|stop|log
"""
import time
import traceback
from datetime import datetime

import gcal

EVERY = 300          # секунд между проверками
AHEAD = 60           # за сколько минут напоминать
DIGEST_AT = "08:00"  # утренняя сводка по Москве


def log(*a):
    print(datetime.now(gcal.TZ).strftime("%d.%m %H:%M"), *a, flush=True)


def main():
    start = datetime.now(gcal.TZ)
    # если запустились уже после утра — сегодняшнюю сводку не шлём
    digest_day = start.date() if start.strftime("%H:%M") >= DIGEST_AT else None
    log("сторож запущен")
    while True:
        try:
            now = datetime.now(gcal.TZ)
            r = gcal.remind(AHEAD)
            if not r.startswith("Напоминать не"):
                log(r)
            if now.strftime("%H:%M") >= DIGEST_AT and digest_day != now.date():
                digest_day = now.date()
                log("сводка:", gcal.digest().replace("\n", " | "))
        except Exception:
            log("ошибка:\n" + traceback.format_exc())
        time.sleep(EVERY)


def daemonize():
    """Отцепиться от того, кто нас запустил: свой сеанс, вывод в лог."""
    import os
    import sys
    from pathlib import Path
    here = Path(__file__).parent
    pidfile = here / "gcal_watch.pid"
    if pidfile.exists():
        try:
            os.kill(int(pidfile.read_text()), 0)
            print(f"уже работает (pid {pidfile.read_text().strip()})")
            return False
        except (OSError, ValueError):
            pass
    if os.fork():
        return False
    os.setsid()
    if os.fork():
        os._exit(0)
    log_fd = os.open(here / "gcal_watch.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    pidfile.write_text(str(os.getpid()))
    return True


if __name__ == "__main__":
    import sys
    if "--daemon" in sys.argv:
        if daemonize():
            main()
    else:
        main()
