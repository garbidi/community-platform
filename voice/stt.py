#!/usr/bin/env python3
"""Расшифровка одного голосового сообщения.

Запускается окружением сборщика: там стоит faster-whisper и настроена
видеокарта. Печатает распознанный текст в stdout — больше ничего.

    /opt/vershina/venvs/vershina-kb/bin/python voice/stt.py /tmp/voice.oga
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

KB_SRC = Path(os.environ.get("VERSHINA_KB_SRC",
                             Path(__file__).resolve().parent.parent.parent / "vershina-kb"))
sys.path.insert(0, str(KB_SRC))

from kb import transcribe          # noqa: E402
from kb.config import Config       # noqa: E402

_model = None


def text_of(path: str) -> str:
    global _model
    cfg = Config.load()
    if _model is None:
        # Загрузчик рассказывает про модель и видеокарту в stdout, а там у нас
        # только распознанный текст — уводим его разговоры в stderr.
        with contextlib.redirect_stdout(sys.stderr):
            _model = transcribe.load_model(cfg)
    return (transcribe.transcribe_file(_model, path) or {}).get("text", "").strip()


def main() -> int:
    if len(sys.argv) < 2:
        print("нужен путь к файлу", file=sys.stderr)
        return 2
    text = text_of(sys.argv[1])
    if not text:
        print("", end="")
        return 1
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
