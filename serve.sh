#!/usr/bin/env bash
# Запуск и остановка локального сервера. PID хранится в файле рядом с базой.
set -u
cd "$(dirname "$0")"
PIDFILE="${VERSHINA_PIDFILE:-/tmp/vershina-app.pid}"
LOG="${VERSHINA_LOG:-/tmp/vershina-app.log}"
PORT="${PORT:-8000}"
# На своей машине слушаем только петлю, в контейнере — все адреса:
# наружу всё равно только через обратный прокси с TLS.
HOST="${VERSHINA_HOST:-127.0.0.1}"
# Разборы делает установленный Claude Code на подписке: приложению
# ключ API не нужен. Очередь вручную — VERSHINA_ANALYSIS=queue.
export VERSHINA_ANALYSIS="${VERSHINA_ANALYSIS:-cli}"

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
    setsid nohup python3 -m uvicorn app.main:app --host "$HOST" --port "$PORT" \
      > "$LOG" 2>&1 < /dev/null &
    echo $! > "$PIDFILE"
    sleep 2
    echo "http://$HOST:$PORT  (лог: $LOG, pid $(cat "$PIDFILE"))"
    ;;
  stop) stop ;;
  restart) "$0" stop; "$0" start ;;
  log) tail -n 40 "$LOG" ;;
  *) echo "использование: $0 {start|stop|restart|log}"; exit 1 ;;
esac
