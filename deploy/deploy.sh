#!/usr/bin/env bash
# Развёртывание помощника на сервере. Запускает тот, кто ставит, — не хозяин.
#
#   ./deploy.sh init     подготовить .env (один раз)
#   ./deploy.sh up       собрать и поднять
#   ./deploy.sh link     показать ссылку на мастер настройки
#   ./deploy.sh logs     смотреть, что происходит
#   ./deploy.sh down     остановить
#
# После ./deploy.sh up остаётся отдать хозяину ссылку из ./deploy.sh link —
# дальше он всё делает сам в браузере.
set -eu
cd "$(dirname "$0")"

COMPOSE="docker compose"
command -v docker >/dev/null || { echo "docker не установлен"; exit 1; }

# Откуда взять ключи телеграма, если они уже есть на этой машине.
LOCAL_KB_ENV="${VERSHINA_LOCAL_KB_ENV:-../../vershina-kb/.env}"

init() {
  if [ -f .env ]; then
    echo ".env уже есть — оставляю как есть"
  else
    cp env.example .env
    echo "создан .env из образца"
  fi

  # Ключи приложения телеграма общие, поэтому переносим их с рабочей
  # машины, а не просим вводить заново.
  if [ -f "$LOCAL_KB_ENV" ]; then
    for key in TG_API_ID TG_API_HASH; do
      have="$(grep -E "^$key=.+" .env || true)"
      [ -n "$have" ] && continue
      val="$(grep -E "^$key=" "$LOCAL_KB_ENV" | head -1 | cut -d= -f2- || true)"
      if [ -n "$val" ]; then
        # sed по месту: ключ в образце присутствует, но пустой
        sed -i "s|^$key=.*|$key=$val|" .env
        echo "$key перенесён из $LOCAL_KB_ENV"
      fi
    done
  else
    echo "рабочего $LOCAL_KB_ENV рядом нет — TG_API_ID и TG_API_HASH впишите в .env сами"
  fi

  echo
  echo "Осталось проверить в .env:"
  echo "  VERSHINA_DOMAIN — домен уже должен указывать на этот сервер"
  echo "  VERSHINA_OWNER  — имя хозяина в именительном падеже"
  echo
  echo "Потом: ./deploy.sh up"
}

check_env() {
  [ -f .env ] || { echo "нет .env — начните с ./deploy.sh init"; exit 1; }
  # shellcheck disable=SC1091
  set -a; . ./.env; set +a
  for key in VERSHINA_DOMAIN TG_API_ID TG_API_HASH; do
    eval "val=\${$key:-}"
    [ -n "$val" ] || { echo "в .env не заполнен $key"; exit 1; }
  done
  case "${VERSHINA_DOMAIN}" in
    *.example.com) echo "в .env остался домен из образца — впишите настоящий"; exit 1 ;;
  esac
}

case "${1:-}" in
  init) init ;;

  up)
    check_env
    echo "собираю образ (в первый раз это долго: torch, распознавание речи, Claude Code)"
    $COMPOSE build
    $COMPOSE up -d
    echo
    $COMPOSE ps
    echo
    echo "Готово. Ссылку для хозяина покажет: ./deploy.sh link"
    ;;

  build) check_env; $COMPOSE build ;;
  down)  $COMPOSE down ;;
  logs)  shift; $COMPOSE logs -f "${@:-}" ;;
  ps)    $COMPOSE ps ;;
  shell) $COMPOSE run --rm --entrypoint /opt/vershina-app/deploy/entrypoint.sh web shell ;;

  link)
    check_env
    token="$($COMPOSE exec -T web python3 -c "
import sys; sys.path.insert(0, '/opt/vershina-app')
from app import setup
print(setup.setup_link_token())
" 2>/dev/null | tr -d '\r\n')"
    if [ -z "$token" ]; then
      echo "не удалось получить ссылку — поднято ли приложение? ./deploy.sh ps"
      exit 1
    fi
    echo
    echo "Отправьте хозяину эту ссылку:"
    echo
    echo "    https://$VERSHINA_DOMAIN/setup?key=$token"
    echo
    echo "Ссылка одноразовая: после настройки она перестанет работать."
    ;;

  *)
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
