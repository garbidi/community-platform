# Один образ на все службы: веб, бот, зум, расписание. Роль выбирается
# командой в docker-compose, а не отдельными образами — контур небольшой,
# а собирать и обновлять одну сборку заметно проще.
#
# Три окружения внутри повторяют нынешнюю раскладку (см. app/speech.py):
#   системный python  — веб и синтез речи (torch + Silero)
#   /opt/venvs/bot    — телеграм-бот и скрипты рабочего каталога
#   /opt/venvs/kb     — выгрузка телеграма и распознавание речи
# Разделение намеренное: у сборщика и у бота конфликтующие зависимости.

FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Системные пакеты. ffmpeg нужен голосовым сообщениям, остальное — база.
# Chromium и виртуальный экран не ставим: бот для записи встреч в первую
# версию не вошёл. Когда понадобится — сюда вернутся chromium и xvfb.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg tini \
        python3 python3-pip python3-venv \
        ffmpeg \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

# Node нужен только ради Claude Code: ядро помощника ходит через CLI.
RUN curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

# --- системный python: веб и синтез речи ---
# torch с индекса CPU-сборок: вариант по умолчанию тянет CUDA и раздувает
# образ на несколько гигабайт, а видеокарты на сервере всё равно нет.
# PyPI вторым индексом обязателен: зеркало torch отдаёт не все зависимости
# колёсами, и без него pip падает на сборке typing_extensions (нет flit_core).
COPY vershina-app/deploy/requirements/system.txt /tmp/system.txt
RUN pip3 install --break-system-packages \
        --index-url https://download.pytorch.org/whl/cpu \
        --extra-index-url https://pypi.org/simple \
        torch \
    && pip3 install --break-system-packages -r /tmp/system.txt \
    && rm /tmp/system.txt

# --- окружение бота ---
COPY vershina-app/deploy/requirements/bot.txt /tmp/bot.txt
RUN python3 -m venv /opt/venvs/bot \
    && /opt/venvs/bot/bin/pip install --upgrade pip \
    && /opt/venvs/bot/bin/pip install -r /tmp/bot.txt \
    && rm /tmp/bot.txt

# --- окружение сборщика ---
# --system-site-packages, чтобы torch и numpy брались системные: второй
# экземпляр torch добавил бы к образу ещё пару гигабайт впустую.
COPY vershina-app/deploy/requirements/kb.txt /tmp/kb.txt
RUN python3 -m venv --system-site-packages /opt/venvs/kb \
    && /opt/venvs/kb/bin/pip install --upgrade pip \
    && /opt/venvs/kb/bin/pip install -r /tmp/kb.txt \
    && rm /tmp/kb.txt

# Работаем не от root: Claude Code и Playwright от рута ведут себя хуже,
# а на томах иначе остаются файлы, которые с хоста не удалить.
RUN useradd --create-home --home-dir /home/vershina --shell /bin/bash vershina

# Исходники обоих проектов: бот с веб-приложением и сборщик базы знаний.
# Секреты и данные сюда не кладутся никогда — только тома (см. .dockerignore).
WORKDIR /opt/vershina-app
COPY --chown=vershina:vershina vershina-app /opt/vershina-app
COPY --chown=vershina:vershina vershina-kb  /opt/vershina-kb

# Каталог данных: тома монтируются внутрь него.
RUN mkdir -p /data/home /data/app /data/kb /data/jarvis /data/backups \
    && chown -R vershina:vershina /data /opt/venvs

USER vershina

# Модель распознавания речи: в сборщике по умолчанию large-v3, но на
# четырёх ядрах без видеокарты она считает в 7,3 раза дольше самой записи
# (замер 21.09.2026 на 47-секундном кружке: 343 с против 70 с у small,
# расшифровка слово в слово та же; base быстрее вдвое, но уже путает слова).
#
# HOME на томе — иначе вход в Claude Code не переживёт перезапуск контейнера.
ENV HOME=/data/home \
    CLAUDE_HOME=/data/home \
    VERSHINA_APP_DIR=/opt/vershina-app \
    VERSHINA_KB_SRC=/opt/vershina-kb \
    VERSHINA_JARVIS_DIR=/data/jarvis \
    VERSHINA_APP_DB=/data/app/app.sqlite \
    VERSHINA_KB_DB=/data/kb/kb.sqlite \
    VERSHINA_KB_DATA=/data/kb \
    KB_DIR=/data/kb \
    VERSHINA_BACKUP_DIR=/data/backups \
    VERSHINA_BOT_ENV=/data/app/bot.env \
    VERSHINA_BOT_PYTHON=/opt/venvs/bot/bin/python \
    VERSHINA_KB_PYTHON=/opt/venvs/kb/bin/python \
    VERSHINA_STT_PYTHON=/opt/venvs/kb/bin/python \
    VERSHINA_TTS_PYTHON=/usr/bin/python3 \
    VERSHINA_STT=/opt/vershina-app/voice/stt.py \
    VERSHINA_HOST=0.0.0.0 \
    WHISPER_DEVICE=cpu \
    WHISPER_COMPUTE=int8 \
    WHISPER_MODEL=small \
    TZ=Europe/Moscow

# tini — чтобы дочерние процессы (синтез речи, распознавание) не оставались
# зомби при остановке контейнера.
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/vershina-app/deploy/entrypoint.sh"]
CMD ["web"]
