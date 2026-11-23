#!/usr/bin/env bash
# Сторож Google Календаря: напоминания в телеграм. Запуск: ./gcal_watch.sh start
set -u
cd "$(dirname "$0")"
PIDFILE="${GCAL_WATCH_PIDFILE:-/tmp/gcal-watch.pid}"
LOG="${GCAL_WATCH_LOG:-/tmp/gcal-watch.log}"
PY="${VERSHINA_BOT_PYTHON:-/opt/venvs/bot/bin/python}"

stop() {
  if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    kill "$(cat "$PIDFILE")" && echo "остановлен (pid $(cat "$PIDFILE"))"
  else
    echo "не запущен"
  fi
  rm -f "$PIDFILE"
}

case "${1:-start}" in
  start)
    stop >/dev/null 2>&1
    setsid nohup "$PY" gcal_watch.py >> "$LOG" 2>&1 < /dev/null &
    echo $! > "$PIDFILE"
    sleep 2
    tail -n 3 "$LOG"
    echo "лог: $LOG, pid $(cat "$PIDFILE")"
    ;;
  stop) stop ;;
  status)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "работает (pid $(cat "$PIDFILE"))"
    else
      echo "не запущен"
    fi ;;
  log) tail -n 40 "$LOG" ;;
  *) echo "использование: $0 {start|stop|status|log}"; exit 1 ;;
esac
