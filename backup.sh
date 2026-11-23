#!/usr/bin/env bash
# Ежедневная копия того, что нельзя потерять: баз, настроек и памяти
# помощника. Помощник умеет запускать код, поэтому единственная надёжная
# защита от «удали всё» — копия, до которой он не дотягивается списком
# запретов, а не сами запреты.
set -eu
DEST="${VERSHINA_BACKUP_DIR:-/opt/vershina/vershina-backups}"
KEEP="${VERSHINA_BACKUP_KEEP:-14}"
DAY="$(date +%F)"
OUT="$DEST/$DAY"
mkdir -p "$OUT"

# Копируем базы средствами самого sqlite: файл может быть открыт на запись,
# и простой cp даёт битую копию. Отдельной утилиты sqlite3 в системе нет,
# поэтому через python — он есть всегда.
PY_BIN="${VERSHINA_PYTHON:-python3}"
# Имена переменных те же, что читает питон-код, чтобы источник правды был один.
APP_DB="${VERSHINA_APP_DB:-/opt/vershina/vershina-app-data/app.sqlite}"
KB_DB="${VERSHINA_KB_DB:-/opt/vershina/vershina-kb-data/kb.sqlite}"
for db in "$APP_DB" "$KB_DB"; do
  [ -f "$db" ] || continue
  "$PY_BIN" - "$db" "$OUT/$(basename "$db")" <<'EOF'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as a, sqlite3.connect(dst) as b:
    a.backup(b)
EOF
done

# Память помощника лежит внутри рабочего каталога и копируется вместе с ним.
JARVIS_DIR="${VERSHINA_JARVIS_DIR:-/opt/vershina/vershina-jarvis}"
BOT_ENV="${VERSHINA_BOT_ENV:-/opt/vershina/src/vershina-app/bot.env}"
cp -a "$JARVIS_DIR" "$OUT/$(basename "$JARVIS_DIR")" 2>/dev/null || true
cp "$BOT_ENV" "$OUT/bot.env" 2>/dev/null || true
chmod -R a-w "$OUT" 2>/dev/null || true

# Старые копии убираем, оставляя последние KEEP дней.
ls -1d "$DEST"/20* 2>/dev/null | sort | head -n -"$KEEP" | while read -r old; do
  chmod -R u+w "$old" 2>/dev/null || true
  rm -rf "$old"
done

echo "$(date '+%d.%m.%Y %H:%M') копия готова: $OUT ($(du -sh "$OUT" | cut -f1))"
