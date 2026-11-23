"""Голос: услышать сказанное и ответить голосом.

Три разных окружения делают три вещи, и это намеренно: тяжёлые модели
не тащатся в бота.

    расшифровка  → окружение сборщика, faster-whisper на видеокарте
    синтез       → системный python, Silero v4 на torch
    упаковка ogg → здесь же, через PyAV

Наружу отсюда торчат три функции: `listen`, `speak` и `wanted`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STT_PY = os.environ.get("VERSHINA_STT_PYTHON", "/opt/vershina/venvs/vershina-kb/bin/python")
TTS_PY = os.environ.get("VERSHINA_TTS_PYTHON", "python3")
STT = ROOT / "voice" / "stt.py"
TTS = ROOT / "voice" / "tts.py"

# button — под ответом кнопка «Прослушать» (по умолчанию);
# auto — голосом в ответ на голосовое; always — всегда; off — никогда.
MODE = os.environ.get("VERSHINA_VOICE", "button")
STT_TIMEOUT = int(os.environ.get("VERSHINA_STT_TIMEOUT", 300))
TTS_TIMEOUT = int(os.environ.get("VERSHINA_TTS_TIMEOUT", 300))
MAX_CHARS = int(os.environ.get("VERSHINA_TTS_MAX", 6000))


def wanted(asked_by_voice: bool) -> bool:
    """Нужно ли озвучивать ответ сразу, не спрашивая."""
    return MODE == "always" or (MODE == "auto" and asked_by_voice)


def offered() -> bool:
    """Нужно ли предлагать озвучку кнопкой."""
    return MODE == "button"


def listen(path: str) -> str:
    """Голосовое, кружок или аудиофайл → текст. Пусто, если речи нет."""
    if not STT.exists():
        return ""
    done = subprocess.run([STT_PY, str(STT), path], capture_output=True,
                          text=True, timeout=STT_TIMEOUT)
    return (done.stdout or "").strip()


class Voice:
    """Постоянный процесс синтеза.

    Модели грузятся четырнадцать секунд, а фраза синтезируется за две.
    Поэтому процесс поднимается один раз и живёт, пока жив бот; если он
    падает, следующий запрос поднимает его заново.
    """

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()

    def _start(self) -> None:
        self.proc = subprocess.Popen(
            [TTS_PY, str(TTS), "--serve"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1)
        ready = self.proc.stdout.readline()
        if not ready or "ready" not in ready:
            raise RuntimeError("процесс синтеза не поднялся")

    def ask(self, text: str, wav: Path) -> float:
        with self.lock:
            for attempt in (1, 2):                 # вторая попытка — с новым процессом
                if self.proc is None or self.proc.poll() is not None:
                    self._start()
                try:
                    self.proc.stdin.write(
                        json.dumps({"text": text, "wav": str(wav)}) + "\n")
                    self.proc.stdin.flush()
                    line = self.proc.stdout.readline()
                    if not line:
                        raise RuntimeError("процесс синтеза замолчал")
                    answer = json.loads(line)
                    if "error" in answer:
                        raise RuntimeError(answer["error"])
                    return float(answer.get("seconds") or 0)
                except (OSError, ValueError, RuntimeError):
                    self.stop()
                    if attempt == 2:
                        raise
            return 0.0

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        self.proc = None


_voice = Voice()


def speak(text: str) -> str | None:
    """Текст → готовое голосовое сообщение (ogg/opus). None, если не вышло."""
    if not TTS.exists() or not text.strip():
        return None
    work = Path(tempfile.mkdtemp(prefix="vershina-voice-"))
    wav, ogg = work / "answer.wav", work / "answer.ogg"
    try:
        _voice.ask(text[:MAX_CHARS], wav)
        if not wav.exists():
            raise RuntimeError("синтез не дал файла")
        _pack(wav, ogg)
        return str(ogg)
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise


def cleanup(path: str | None) -> None:
    if path:
        shutil.rmtree(Path(path).parent, ignore_errors=True)


def _pack(wav: Path, ogg: Path) -> None:
    """wav → ogg/opus: телеграм показывает голосовым только такой формат."""
    import av
    from av.audio.resampler import AudioResampler

    src = av.open(str(wav))
    out = av.open(str(ogg), "w", format="ogg")
    stream = out.add_stream("libopus", rate=48000, layout="mono")
    res = AudioResampler(format="s16", layout="mono", rate=48000)
    try:
        for frame in src.decode(audio=0):
            for chunk in res.resample(frame):
                chunk.pts = None
                for packet in stream.encode(chunk):
                    out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    finally:
        out.close()
        src.close()
