#!/usr/bin/env bash
# Запуск и остановка телеграм-помощника. Настройки — в bot.env рядом.
set -u
cd "$(dirname "$0")"
PIDFILE="${VERSHINA_BOT_PIDFILE:-/tmp/vershina-bot.pid}"
LOG="${VERSHINA_BOT_LOG:-/tmp/vershina-bot.log}"
PY="${VERSHINA_BOT_PYTHON:-/opt/vershina/venvs/vershina-bot/bin/python}"

[ -f bot.env ] && set -a && . ./bot.env && set +a

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
    setsid nohup "$PY" bot.py > "$LOG" 2>&1 < /dev/null &
    echo $! > "$PIDFILE"
    sleep 2
    tail -n 3 "$LOG"
    echo "лог: $LOG, pid $(cat "$PIDFILE")"
    ;;
  stop) stop ;;
  restart) "$0" stop; "$0" start ;;
  log) tail -n 40 "$LOG" ;;
  ask) shift; "$PY" ask.py "$@" ;;
  *) echo "использование: $0 {start|stop|restart|log|ask <вопрос>}"; exit 1 ;;
esac
