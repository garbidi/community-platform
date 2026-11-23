#!/usr/bin/env bash
# Точка входа контейнера. Роль передаётся первым аргументом:
#   web   — веб-приложение и мастер настройки (работает всегда)
#   bot   — телеграм-бот (ждёт, пока в мастере появится токен)
#   cron  — ночной прогон и резервные копии
#
# Службы, которым нужны настройки, не падают в цикле перезапуска, пока
# настроек нет: они ждут и сами поднимаются, как только мастер их запишет.
set -u

ROLE="${1:-web}"
APP_DIR="${VERSHINA_APP_DIR:-/opt/vershina-app}"
JARVIS="${VERSHINA_JARVIS_DIR:-/data/jarvis}"
BOT_ENV="${VERSHINA_BOT_ENV:-/data/app/bot.env}"
SKEL="$APP_DIR/deploy/jarvis"

log() { echo "$(date '+%d.%m.%Y %H:%M:%S') [$ROLE] $*"; }

# --- подготовка рабочего каталога ---------------------------------------
# Скрипты приезжают из образа и обновляются с каждой сборкой. Данные
# (память, записи, токены) живут на томе, и их не трогаем никогда.
prepare() {
  mkdir -p "$JARVIS" "$JARVIS/recordings" "$JARVIS/meetings" "$JARVIS/incoming"
  for f in "$SKEL"/*; do
    [ -e "$f" ] || continue
    cp -f "$f" "$JARVIS/$(basename "$f")"
  done
  chmod +x "$JARVIS"/*.sh 2>/dev/null || true

  # CLAUDE.md, права и доверие каталогу. Существующее не перезаписываем:
  # инструкции мог поправить хозяин.
  python3 "$APP_DIR/deploy/render_config.py" \
      --owner "${VERSHINA_OWNER:-хозяин}" \
      --jarvis "$JARVIS" \
      --app "$APP_DIR" \
      --kb "${VERSHINA_KB_SRC:-/opt/vershina-kb}" \
      --bot-python "${VERSHINA_BOT_PYTHON:-/opt/venvs/bot/bin/python}" \
      --kb-python "${VERSHINA_KB_PYTHON:-/opt/venvs/kb/bin/python}" \
      --claude-home "${CLAUDE_HOME:-$HOME}" || {
    log "не удалось собрать настройки рабочего каталога"; return 1; }
}

# --- ожидание настроек ---------------------------------------------------
# Ждём не только токен, но и хозяина. Запущенный бот забирает входящие
# сообщения себе, и шаг «Это вы» в мастере перестаёт их видеть.
have_token() {
  [ -f "$BOT_ENV" ] && grep -q '^VERSHINA_BOT_TOKEN=.\+' "$BOT_ENV" \
    && grep -q '^VERSHINA_BOT_ALLOW=.\+' "$BOT_ENV"
}

wait_for_setup() {
  local said=0
  until have_token; do
    if [ "$said" = 0 ]; then
      log "жду, пока настройку закончат в мастере — служба поднимется сама"
      said=1
    fi
    sleep 10
  done
  log "настройки на месте, запускаюсь"
}

stamp() { stat -c %Y "$BOT_ENV" 2>/dev/null || echo 0; }

# Настройки читаем в окружение: bot.py и скрипты рабочего каталога берут
# токен оттуда. Обязательно после ожидания — до него файла ещё нет.
load_env() {
  set -a
  [ -f "$BOT_ENV" ] && . "$BOT_ENV"
  set +a
}

# Запускает команду и перезапускает её, если настройки в мастере поменяли.
# Так веб-контейнеру не нужен доступ к докер-сокету, чтобы поднять соседа.
run_watched() {
  while true; do
    local before; before="$(stamp)"
    load_env
    "$@" &
    local pid=$!
    while kill -0 "$pid" 2>/dev/null; do
      sleep 5
      if [ "$(stamp)" != "$before" ]; then
        log "настройки изменились — перезапускаюсь"
        kill "$pid" 2>/dev/null
        wait "$pid" 2>/dev/null
        break
      fi
    done
    wait "$pid" 2>/dev/null
    local code=$?
    if [ "$(stamp)" = "$before" ]; then
      log "процесс завершился (код $code), поднимаю снова через 10 с"
      sleep 10
    fi
  done
}

# --- роли ----------------------------------------------------------------
prepare || exit 1
load_env

case "$ROLE" in
  web)
    log "веб-приложение на ${VERSHINA_HOST:-0.0.0.0}:${PORT:-8000}"
    exec python3 -m uvicorn app.main:app \
        --host "${VERSHINA_HOST:-0.0.0.0}" --port "${PORT:-8000}" \
        --app-dir "$APP_DIR"
    ;;

  bot)
    wait_for_setup
    load_env
    cd "$APP_DIR" || exit 1
    run_watched "${VERSHINA_BOT_PYTHON:-/opt/venvs/bot/bin/python}" bot.py
    ;;

  cron)
    wait_for_setup
    load_env
    log "расписание: ночной прогон 04:30, копия 05:00 (МСК)"
    last=""
    while true; do
      now="$(date '+%H:%M')"; today="$(date '+%F')"
      if [ "$now" = "04:30" ] && [ "$last" != "daily-$today" ]; then
        last="daily-$today"
        log "ночной прогон"
        "${VERSHINA_KB_SRC:-/opt/vershina-kb}/daily.sh" || log "прогон завершился с ошибкой"
      fi
      if [ "$now" = "05:00" ] && [ "$last" != "backup-$today" ]; then
        last="backup-$today"
        log "резервная копия"
        "$APP_DIR/backup.sh" || log "копия не удалась"
      fi
      sleep 30
    done
    ;;

  shell) exec bash ;;

  *)
    echo "неизвестная роль: $ROLE (нужна одна из: web, bot, cron, shell)"
    exit 1
    ;;
esac
