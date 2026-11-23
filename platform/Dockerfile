# Платформа Вершины на сервере. Основа — образ Стенографа: в нём уже есть
# Python, GigaAM и разделение голосов (расшифровка разборов) и Claude Code
# (выжимки). Сверху — только веб-библиотеки приложения.
FROM stenograf-app:latest

RUN pip install --break-system-packages "jinja2>=3.1" "python-multipart>=0.0.9" openpyxl "telethon>=1.36"

WORKDIR /platform
COPY app /platform/app
COPY mentors_import.py /platform/
COPY kb /vershina-kb/kb

ENV VERSHINA_APP_DB=/data/app.sqlite \
    VERSHINA_KB_DB=/data/kb/kb.sqlite \
    VERSHINA_KB_SRC=/vershina-kb \
    VERSHINA_CALLS_DIR=/data/calls \
    VERSHINA_REVIEW_DIR=/data/reviews \
    VERSHINA_ANALYSIS_DIR=/data/analysis \
    VERSHINA_FLOW_CONFIG=/data/mm7.json \
    VERSHINA_ANALYSIS=cli \
    VERSHINA_STT_PYTHON=python3 \
    VERSHINA_STT_SCRIPT=/app/stt_speakers.py \
    STENO_GIGAAM_DIR=/data/gigaam \
    STENO_DEVICE=cpu
EXPOSE 8000
# четыре процесса: один процесс Python упирается в одно ядро, и при 25 одновременных
# пользователях ответы шли по 6–7 с (нагрузочный тест 19.10). Фоновые задачи —
# только в одном из них (app/proclock.py)
CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
