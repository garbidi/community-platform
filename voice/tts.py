#!/usr/bin/env python3
"""Синтез речи: текст ответа → wav-файл.

Запускается системным python: там стоит torch. Модель — Silero v4 русский.

Две вещи, которых у Silero нет из коробки и которые слышны сразу:

* **ударения.** Свой словарь у модели неполный, и получается «на связ+и»
  вместо «на св+язи». Поэтому текст сначала проходит через RUAccent,
  который ставит ударения по словарю и, для слов вне его, нейросетью —
  включая имена («Петр+ова», «Сидор+енко») и омографы («з+амок на горе»
  против «зам+ок на двери»).
* **вопрос.** Интонация вопроса у модели слабая, и разметкой это не
  чинится — см. комментарий у `pieces`. Нужен другой движок.

Модели грузятся долго — двадцать секунд на расстановщик ударений, — поэтому
обычный режим «один запуск на одну фразу» годится только для проверок.
В работе бот держит постоянный процесс:

    python3 voice/tts.py "текст" /tmp/answer.wav     разовый синтез
    python3 voice/tts.py --serve                     постоянный процесс
"""

from __future__ import annotations

import os
import re
import sys
import wave

SPEAKER = os.environ.get("VERSHINA_TTS_SPEAKER", "eugene")   # мужской голос
RATE = int(os.environ.get("VERSHINA_TTS_RATE", 48000))
ACCENTS = os.environ.get("VERSHINA_TTS_ACCENTS", "1") != "0"
CHUNK = 800            # силеро плохо тянет очень длинные куски
LIMIT = 6000           # больше двух минут речи в голосовом ответе не нужно

# Эмодзи и разметка в синтезе превращаются в мусор или паузы.
JUNK = re.compile(r"[*_`#>|]|[\U0001F000-\U0001FAFF☀-➿️⃣]")


def clean(text: str) -> str:
    text = JUNK.sub(" ", text)
    text = re.sub(r"https?://\S+", "ссылка", text)
    # цифры и латиницу модель молча глотает — переписываем словами
    from spoken import spoken
    text = spoken(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:LIMIT]


def _patch_accent_model() -> None:
    """Починка расстановщика ударений для слов вне словаря.

    Его onnx-модель ждёт три входа, а токенизатор отдаёт два, и на каждом
    незнакомом слове всё падает. Досыпаем недостающий вход нулями — это то
    же, что модель получила бы при обучении на одном предложении.
    """
    import numpy as np
    from ruaccent.accent_model import AccentModel

    original = AccentModel.put_accent

    def put_accent(self, word):
        try:
            return original(self, word)
        except ValueError as exc:
            if "token_type_ids" not in str(exc):
                raise
            inputs = self.tokenizer(word.lower(), return_tensors="np")
            inputs = {k: v.astype(np.int64) for k, v in inputs.items()}
            inputs["token_type_ids"] = np.zeros_like(inputs["input_ids"])
            out = self.session.run(None, inputs)
            names = {o.name: i for i, o in enumerate(self.session.get_outputs())}
            logits = out[names["logits"]]
            e = np.exp(logits - logits.max(axis=-1, keepdims=True))
            probs = e / e.sum(axis=-1, keepdims=True)
            scores = np.max(probs, axis=-1)[0]
            labels = np.argmax(logits, axis=-1)[0]
            pred = [{"label": self.id2label[str(label)], "score": float(score)}
                    for label, score in zip(labels, scores)]
            return self.render_stress(word, pred)

    AccentModel.put_accent = put_accent


_accentor = None


def accented(text: str) -> str:
    global _accentor
    if not ACCENTS:
        return text
    try:
        if _accentor is None:
            _patch_accent_model()
            from ruaccent import RUAccent
            _accentor = RUAccent()
            _accentor.load(omograph_model_size="turbo", use_dictionary=True)
        return _accentor.process_all(text)
    except Exception as exc:          # без ударений хуже, но не молчание
        print(f"ударения пропущены: {exc}", file=sys.stderr)
        return text


def sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?…])\s+", text) if s.strip()]


# SSML-разметка для вопросов здесь была и убрана намеренно. Замеры высоты
# тона показали, что своя интонация у модели лучше: на «Ты уже посмотрел
# отчёт?» она поднимает тон к концу на 49 Гц, а с <prosody> — только на 11.
# Тег заставляет читать слово отдельным куском и гасит естественный подъём.
# Внятной вопросительной интонации у этой модели нет в принципе: подъём
# есть, но он не похож на русский вопрос. Это лечится сменой движка,
# а не разметкой.


def pieces(text: str) -> list[str]:
    """Резать по предложениям: так паузы падают на точки, а не посередине."""
    out, cur = [], ""
    for part in sentences(text):
        if len(cur) + len(part) + 1 > CHUNK and cur:
            out.append(cur.strip())
            cur = part
        else:
            cur = f"{cur} {part}".strip()
    if cur:
        out.append(cur)
    return out


_model = None


def load_model():
    """Silero грузится из кэша torch.hub — быстро, но один раз за процесс."""
    global _model
    if _model is None:
        import torch

        torch.set_num_threads(int(os.environ.get("VERSHINA_TTS_THREADS", 4)))
        model, _example = torch.hub.load("snakers4/silero-models", "silero_tts",
                                         language="ru", speaker="v4_ru",
                                         trust_repo=True)
        _model = model
    return _model


def synth(text: str, path: str) -> float:
    import torch

    model = load_model()
    parts = []
    for piece in pieces(accented(clean(text))):
        if not piece:
            continue
        try:
            parts.append(model.apply_tts(text=piece, speaker=SPEAKER,
                                         sample_rate=RATE))
        except Exception as exc:
            print(f"кусок пропущен: {exc}", file=sys.stderr)
    if not parts:
        raise RuntimeError("нечего озвучивать")

    audio = torch.cat(parts) if len(parts) > 1 else parts[0]
    pcm = (audio.clamp(-1, 1) * 32767).to(torch.int16).numpy().tobytes()
    with wave.open(path, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(RATE)
        f.writeframes(pcm)
    return len(audio) / RATE


def serve() -> int:
    """Постоянный процесс: строка JSON на входе — строка JSON на выходе.

    Модели загружаются один раз, дальше каждая фраза стоит секунды.
    Пустая строка или закрытый вход — конец работы.
    """
    import json

    warm()
    print(json.dumps({"ready": True}), flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            break
        try:
            task = json.loads(line)
            seconds = synth(task["text"], task["wav"])
            answer = {"seconds": round(seconds, 1)}
        except Exception as exc:
            answer = {"error": f"{type(exc).__name__}: {exc}"[:300]}
        print(json.dumps(answer, ensure_ascii=False), flush=True)
    return 0


def warm() -> None:
    """Прогреть обе модели заранее, до первой фразы."""
    load_model()
    accented("прогрев")


def main() -> int:
    if "--serve" in sys.argv[1:]:
        return serve()
    if len(sys.argv) < 3:
        print("использование: tts.py «текст» файл.wav | tts.py --serve",
              file=sys.stderr)
        return 2
    seconds = synth(sys.argv[1], sys.argv[2])
    print(f"{seconds:.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
