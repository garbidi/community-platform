"""Отбор того, что вообще стоит показывать модели.

Три наблюдения из корпуса, на которых всё построено:

1. Сорок процентов сообщений — куски одной мысли: человек пишет очередями
   по 3–10 реплик подряд. Склейка серий возвращает связность и убирает
   четверть строк вместе со служебной разметкой.
2. Шум занимает место в списке, но не в объёме: «да», «+», «доброе утро» —
   это 10% реплик и 0,7% знаков. Выкидывать их ради экономии бессмысленно,
   ради читаемости — стоит.
3. Настоящая проблема не в шуме, а в обрезке: у активного участника
   130–170 тысяч знаков, а в модель влезает 40. Сейчас берутся последние
   по времени, то есть две недели из пяти месяцев. Отбор по ценности
   покрывает весь период.

Метки здесь ставятся правилами и намеренно скупые: они отделяют служебное
от содержательного и отмечают форму (вопрос, цифры, голос, ссылка).
Смысловую разметку («знание», «обязательство», «личное») правилами
не сделать — для неё есть второй слой в marks.py, где метку ставит модель
и результат ложится в базу.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from datetime import timedelta
from typing import Any, Iterable

from . import corpus, store
from .corpus import kb_build

BURST_GAP = timedelta(minutes=3)   # пауза, после которой начинается новая реплика

WORD = re.compile(r"[^\W\d_]+", re.UNICODE)

RX = {
    "символы": re.compile(r"^[\W\d_\s]+$", re.UNICODE),
    "реакция": re.compile(
        r"^\W*(красавчик|красотка|огонь|топ|браво|спасибо|благодарю|супер|мощно|"
        r"горжусь|молодец|класс|респект|ееее+|ура+|да|нет|факт|ок|окей|\+|100%)\W*$", re.I),
    "ритуал": re.compile(
        r"^\W*(всем\s+)?(доброе утро|добрый день|добрый вечер|привет|здравствуйте|"
        r"доброго|спокойной ночи|с добрым утром)\W*", re.I),
    "вопрос": re.compile(r"\?"),
    "ссылка": re.compile(r"https?://|t\.me/"),
    "цифры": re.compile(r"\d[\d\s.,]*\s*(₽|руб|млн|тыс|%|шт|штук|дней|человек)", re.I),
    "организация": re.compile(
        r"(подключиться|zoom\.us|штраф|обязательн[ао] для всех|в \d{1,2}:\d{2}|"
        r"созвон в|встреча в|начало в)", re.I),
}


# ------------------------------------------------------------------ реплики

def merge_bursts(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Склеить подряд идущие сообщения одного автора в одну реплику."""
    out: list[dict[str, Any]] = []
    i = 0
    seq = sorted(messages, key=lambda r: r["date"])
    while i < len(seq):
        j = i + 1
        while (j < len(seq) and seq[j]["user_id"] == seq[i]["user_id"]
               and kb_build._dt(seq[j]["date"]) - kb_build._dt(seq[j - 1]["date"]) < BURST_GAP):
            j += 1
        part = seq[i:j]
        out.append({
            "chat_id": part[0]["chat_id"],
            "msg_id": part[0]["msg_id"],
            "ids": [p["msg_id"] for p in part],
            "user_id": part[0]["user_id"],
            "date": part[0]["date"],
            "text": "\n".join(p["content"] for p in part),
            "kinds": {p["media_kind"] for p in part if p["media_kind"]},
            "parts": len(part),
        })
        i = j
    return out


# ------------------------------------------------------------------ признаки

def marks_of(rep: dict[str, Any]) -> set[str]:
    """Что можно утверждать о реплике, не понимая её смысла."""
    text = rep["text"].strip()
    words = WORD.findall(text)
    marks: set[str] = set()

    if RX["символы"].match(text) or (len(words) <= 1 and not re.search(r"\d", text)):
        marks.add("шум")
    if RX["реакция"].match(text):
        marks.add("реакция")
    if RX["ритуал"].match(text) and len(words) <= 4:
        marks.add("ритуал")
    if RX["вопрос"].search(text) and len(words) >= 3:
        marks.add("вопрос")
    if RX["цифры"].search(text):
        marks.add("цифры")
    if RX["ссылка"].search(text):
        marks.add("ссылка")
    if RX["организация"].search(text):
        marks.add("организация")
    if rep["kinds"] & {"voice", "video_note"}:
        marks.add("голос")
    if len(words) >= 40:
        marks.add("развёрнутое")
    return marks or {"обычное"}


def _corpus_stats() -> dict[str, Any]:
    """Отклик на сообщение и частота шаблонных фраз — считаются один раз на корпус."""
    d = corpus._data()
    key = d.get("_signal_stamp")
    if key is not None:
        return d["_signal"]

    answers: Counter = Counter()
    speakers: defaultdict[tuple[int, int], set[int]] = defaultdict(set)
    phrases: Counter = Counter()
    for cid, rows in d["rows_by_chat"].items():
        for r in rows:
            if r.get("reply_to"):
                answers[(cid, r["reply_to"])] += 1
                if r["user_id"]:
                    speakers[(cid, r["reply_to"])].add(r["user_id"])
            if r["content"]:
                phrases[r["content"].strip().lower()[:80]] += 1

    # Слова, встречающиеся почти везде, ничего не говорят о конкретной реплике.
    doc_freq: Counter = Counter()
    docs = 0
    for rows in d["rows_by_chat"].values():
        for r in rows:
            if r["content"]:
                docs += 1
                doc_freq.update(set(w.lower() for w in WORD.findall(r["content"])))

    stats = {"answers": answers, "speakers": speakers, "phrases": phrases,
             "doc_freq": doc_freq, "docs": max(1, docs)}
    d["_signal"] = stats
    d["_signal_stamp"] = True
    return stats


def score_of(rep: dict[str, Any], marks: set[str], stats: dict[str, Any]) -> float:
    """Оценка 0…1: насколько реплика вероятно несёт содержание.

    Ни одна составляющая не про смысл — это объём мысли, реакция людей,
    редкость слов и форма. Смысл добавляет второй слой, если он включён.
    """
    words = WORD.findall(rep["text"])
    if not words:
        return 0.0

    value = min(1.0, math.log1p(len(words)) / math.log(60)) * 0.35

    answers = max((stats["answers"].get((rep["chat_id"], m), 0) for m in rep["ids"]), default=0)
    people = max((len(stats["speakers"].get((rep["chat_id"], m), ())) for m in rep["ids"]), default=0)
    value += min(1.0, answers / 3) * 0.20 + min(1.0, people / 3) * 0.10

    # доля слов, которые редки для этого сообщества
    rare = sum(1 for w in {w.lower() for w in words}
               if stats["doc_freq"].get(w, 0) / stats["docs"] < 0.005)
    value += min(1.0, rare / max(4, len(set(words)))) * 0.15

    for mark, weight in (("развёрнутое", 0.12), ("голос", 0.12), ("цифры", 0.08),
                         ("вопрос", 0.06), ("организация", -0.05), ("ссылка", -0.02),
                         ("реакция", -0.25), ("ритуал", -0.30), ("шум", -0.40)):
        if mark in marks:
            value += weight

    if stats["phrases"].get(rep["text"].strip().lower()[:80], 0) > 5:
        value -= 0.15          # фраза ходит по чату у всех, личного в ней мало

    return max(0.0, min(1.0, value))


def model_labels() -> dict[tuple[int, int], tuple[str, int]]:
    """Смысловые метки второго слоя, если они уже проставлены (marks.py)."""
    try:
        conn = store.connect()
        rows = conn.execute("SELECT chat_id, msg_id, label, usefulness FROM reply_marks")
        out = {(r["chat_id"], r["msg_id"]): (r["label"], r["usefulness"]) for r in rows}
        conn.close()
        return out
    except Exception:
        return {}


# Вклад смысловой метки в оценку. Польза важнее самой метки: «болтовня»
# с пользой 2 бывает информативнее «знания» с пользой 1.
USEFULNESS_WEIGHT = {3: 0.30, 2: 0.12, 1: 0.0, 0: -0.35}
LABEL_WEIGHT = {"личное": 0.10, "обязательство": 0.10, "знание": 0.08,
                "спор": 0.08, "результат": 0.06, "болтовня": -0.10}


def replies_of(tg_id: int) -> list[dict[str, Any]]:
    """Все реплики участника: склеенные, с метками и оценкой."""
    rows, _ = corpus.messages_of(tg_id, limit=1_000_000)
    if not rows:
        return []
    src = []
    for m in rows:
        src.append({"chat_id": m["chat_id"], "msg_id": m["msg_id"], "user_id": m["user_id"],
                    "date": m["date"].isoformat(), "content": m["text"],
                    "media_kind": "voice" if m["kind"] == "голосовое"
                    else "video_note" if m["kind"] == "кружок" else None})
    stats = _corpus_stats()
    labels = model_labels()
    out = []
    for rep in merge_bursts(src):
        rep["marks"] = marks_of(rep)
        rep["score"] = score_of(rep, rep["marks"], stats)
        mark = labels.get((rep["chat_id"], rep["msg_id"]))
        if mark:
            rep["label"], rep["usefulness"] = mark
            rep["score"] = max(0.0, min(1.0, rep["score"]
                                        + USEFULNESS_WEIGHT.get(rep["usefulness"], 0.0)
                                        + LABEL_WEIGHT.get(rep["label"], 0.0)))
        rep["when"] = kb_build._dt(rep["date"]).astimezone(corpus.MSK)
        out.append(rep)
    return out


# ------------------------------------------------------------------ отбор

LINE_OVERHEAD = 34     # «[03.09.2026 21:40 · кружок · 4 ответа] »

# Доли бюджета: голос, содержание, свежесть, образцы речи.
SHARE_VOICE, SHARE_VALUE, SHARE_RECENT, SHARE_STYLE = 0.25, 0.40, 0.20, 0.15


def select(replies: list[dict[str, Any]], limit: int) -> tuple[list[dict], dict[str, Any]]:
    """Набрать материал в пределах бюджета знаков.

    Только по оценке брать нельзя: получится подборка длинных монологов,
    из которой не видно, как человек разговаривает. Поэтому бюджет делится
    на три части — содержание, последние недели и образцы коротких реплик.
    """
    if not replies:
        return [], {}

    cost = lambda r: len(r["text"]) + LINE_OVERHEAD
    chosen: dict[tuple[int, int], dict] = {}
    spent = 0

    def take(pool: Iterable[dict], budget: int) -> None:
        nonlocal spent
        used = 0
        for r in pool:
            key = (r["chat_id"], r["msg_id"])
            if key in chosen:
                continue
            c = cost(r)
            if used + c > budget:
                continue
            chosen[key] = r
            used += c
            spent += c

    # Голосовые и кружки — отдельной строкой: их мало, они дорого достались
    # (человек записал и наговорил), и только там слышно, как он формулирует.
    voice = sorted((r for r in replies if "голос" in r["marks"]), key=lambda r: -r["score"])
    take(voice, int(limit * SHARE_VOICE))

    by_value = sorted(replies, key=lambda r: -r["score"])
    take(by_value, int(limit * SHARE_VALUE))

    by_recent = sorted(replies, key=lambda r: r["date"], reverse=True)
    take(by_recent, int(limit * SHARE_RECENT))

    # Образцы речи: короткие реплики, равномерно растянутые по всему периоду,
    # чтобы было видно и как он писал в апреле, и как в сентябре.
    short = [r for r in sorted(replies, key=lambda r: r["date"])
             if len(r["text"]) <= 120 and "шум" not in r["marks"]]
    step = max(1, len(short) // 60)
    take(short[::step], limit - spent)

    picked = sorted(chosen.values(), key=lambda r: r["date"])

    left = [r for r in replies if (r["chat_id"], r["msg_id"]) not in chosen]
    dropped_marks: Counter = Counter()
    for r in left:
        for m in r["marks"]:
            dropped_marks[m] += 1
    common = Counter(r["text"].strip().lower() for r in left if len(r["text"]) <= 40)

    report = {
        "replies_total": len(replies),
        "messages_total": sum(r["parts"] for r in replies),
        "picked": len(picked),
        "chars": spent,
        "dropped": len(left),
        "dropped_chars": sum(len(r["text"]) for r in left),
        "dropped_marks": dropped_marks.most_common(),
        "common_phrases": common.most_common(5),
        "covers": (picked[0]["when"].strftime("%d.%m.%Y"),
                   picked[-1]["when"].strftime("%d.%m.%Y")) if picked else None,
    }
    return picked, report


def render(rep: dict[str, Any], stats: dict[str, Any] | None = None) -> str:
    """Одна реплика для промпта: время, вид, метки формы, отклик."""
    stats = stats or _corpus_stats()
    kind = ("кружок" if "video_note" in rep["kinds"]
            else "голосовое" if "voice" in rep["kinds"] else "текст")
    answers = max((stats["answers"].get((rep["chat_id"], m), 0) for m in rep["ids"]), default=0)
    tags = [t for t in ("вопрос", "цифры", "организация") if t in rep["marks"]]
    if rep.get("label"):
        tags.insert(0, rep["label"])
    meta = [rep["when"].strftime("%d.%m.%Y %H:%M"), kind]
    if rep["parts"] > 1:
        meta.append(f"{rep['parts']} сообщ. подряд")
    if tags:
        meta.append(", ".join(tags))
    if answers:
        meta.append(f"ответов: {answers}")
    return f"[{' · '.join(meta)}] {rep['text']}"
