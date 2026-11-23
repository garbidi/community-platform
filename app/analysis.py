"""Разбор участника моделью: во что человек включается и как с ним говорить.

Приложение делает всю подготовительную работу — собирает поведенческие метрики
и высказывания, укладывает их в промпт и кладёт запрос в очередь. Кто именно
отвечает — Claude Code сейчас или другая модель потом — решает провайдер.
Смена движка = одна функция в PROVIDERS, всё остальное не трогается.
"""

from __future__ import annotations

import html
import os
import threading
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import claude_env, corpus, signal, store
from .config import APP_DB

# Куда складываются промпты: рядом с базой приложения, не в репозиторий —
# это выдержки из личной переписки участников.
PROMPT_DIR = Path(os.environ.get("VERSHINA_ANALYSIS_DIR", APP_DB.parent / "analysis"))

# Сколько текста участника уходит в модель. 40 000 знаков — это примерно
# 10–12 тысяч токенов: хватает, чтобы услышать человека, и не разоряет на длинных.
MATERIAL_LIMIT = int(os.environ.get("VERSHINA_ANALYSIS_LIMIT", 40_000))

# Запас под шапку: метрики, объяснение отбора и инструкцию.
HEAD_ALLOWANCE = 3_000

PROVIDER = os.environ.get("VERSHINA_ANALYSIS", "queue")

INSTRUCTION = """Ты помогаешь куратору закрытого бизнес-сообщества «Вершина»
(мастермайнды для предпринимателей). Куратору нужно понять одного участника
по тому, как он ведёт себя в чатах потока, и найти способ включить его в работу.

Ниже — поведенческие метрики этого участника и его высказывания: текстовые
сообщения и расшифровки голосовых и кружков. Больше о человеке ничего не известно.

Напиши разбор на русском языке по этой структуре:

## Как он себя ведёт
Что видно по переписке: как заходит в разговор, коротко или подробно, отвечает
или инициирует, в какое время активен, как реагирует на других. Только
наблюдения — но обобщённые, без пересказа отдельных случаев.

## Что им движет
Что у него вызывает включённость, а что он обходит стороной. Если по данным
видно слабо — так и напиши.

## Как с ним говорить
3–5 пунктов: каким тоном, с чего начинать, о чём спрашивать, какая длина
сообщения до него доходит.

## Что не сработает
2–4 пункта: на что он, судя по поведению, закрывается или не реагирует.

## С чего начать разговор
Два-три готовых первых сообщения куратора этому человеку — живым языком,
про его дело и его задачи в потоке. Не шаблоны, а конкретные реплики.

Жёсткие правила:
— Не цитируй его сообщения и не пересказывай отдельные реплики так, чтобы
  их можно было узнать. Никаких дат, обстоятельств и прямой речи. Выводы
  делаешь по переписке, но в тексте остаются только обобщения.
— Готовые реплики куратора не должны выдавать, что кто-то читал его чаты:
  куратор говорит как человек, который просто знает участника.
— Никаких диагнозов, психиатрических и клинических ярлыков. Пишешь про рабочий
  стиль и поведение, а не про личность как патологию.
— Ничего не выдумывай. Нет данных — пиши «по переписке не видно».
— Не обещай точность: это материал для подготовки к разговору, а не диагностика.
— Только разделы выше, без вступления и заключения."""


PORTRAIT = """Ты пишешь участнику закрытого бизнес-сообщества «Вершина»
(мастермайнды для предпринимателей) его портрет — текст про него самого,
который он прочитает лично.

Ниже — то, как он участвует в потоке: показатели участия и его высказывания.
Больше о человеке ничего не известно.

Напиши на русском языке, обращаясь к нему на «вы», по этой структуре:

## Как вы работаете
Как вы включаетесь в работу, в каком ритме, что делаете первым. Спокойные
наблюдения, без оценок «хорошо» или «плохо».

## В чём ваша сила
2–4 пункта: что у вас получается лучше, чем у большинства, и на что в потоке
стоит опираться.

## Что может тормозить
2–3 пункта: где вы, судя по участию, чаще всего застреваете. Формулируй как
рабочую особенность, а не как недостаток характера.

## Что стоит попробовать в этом потоке
2–3 конкретных шага, которые вытекают из вашего же способа работать.

Жёсткие правила:
— Пишешь человеку о нём, а не о нём третьему лицу. Никаких советов, как с ним
  обращаться, — это его текст, не инструкция для куратора.
— Не цитируй его сообщения и не пересказывай отдельные случаи так, чтобы их
  можно было узнать. Никаких дат, обстоятельств и прямой речи.
— Не упоминай переписку, чаты, сообщения и расшифровки. Источник называется
  «ваше участие в потоке».
— Никаких диагнозов и клинических ярлыков. Речь о рабочем стиле.
— Ничего не выдумывай: нет данных — не пиши. Если по какому-то разделу
  материала не хватает, напиши в нём одну честную фразу, что по участию этого
  пока не видно, и не добирай объём догадками.
— Не обещай точность: это взгляд со стороны, а не диагностика.
— Только разделы выше, без вступления и заключения."""


KINDS = {"ментору": INSTRUCTION, "участнику": PORTRAIT}


# ----------------------------------------------------------------- материал

def _material(tg_id: int) -> tuple[str, int, dict[str, Any]]:
    """Метрики и отобранные высказывания участника в виде текста для модели."""
    prof = corpus.profile_of(tg_id)
    if not prof:
        raise ValueError(f"участник {tg_id} не найден в базе знаний")

    # Сообщения склеиваются в реплики и отбираются по ценности — иначе в бюджет
    # попадают только последние две недели, а у активного участника их пять месяцев.
    replies = signal.replies_of(tg_id)
    picked, report = signal.select(replies, MATERIAL_LIMIT - HEAD_ALLOWANCE)

    head = [
        f"# Участник: {prof['name']}",
        "",
        f"В переписке с {prof['first_seen']} по {prof['last_seen']}, "
        f"состояние на конец выгрузки: {prof['status']}"
        + (f", молчит {prof['silent_days']} дн." if prof["silent_days"] else ""),
        "",
        "## Поведенческие метрики",
        "",
        f"- сообщений: {prof['messages']}, из них голосом (кружки и голосовые): "
        f"{prof['circles'] + prof['voice']}",
        f"- слов всего: {prof['words']}, в среднем на сообщение: {prof['avg_words']}",
        f"- минут речи: {prof['audio_min']}, темп речи: {prof['speech_rate'] or '—'} слов/мин",
        f"- доля пауз в речи: {prof['pause_share'] if prof['pause_share'] is not None else '—'}, "
        f"остановок в минуту: {prof['pauses_per_min'] or '—'}",
        f"- доля сообщений в ответ другим: {prof['reply_share']}, "
        f"медианная задержка ответа: {prof['reply_lag_min'] or '—'} мин",
        f"- доля сообщений с вопросом: {prof['question_share']}",
        f"- пик активности: {prof.get('peak_hour_label') or '—'} МСК, "
        f"доля ночных сообщений: {prof['night_share']}",
        f"- самое долгое молчание: {prof['max_silence_days']} дн., "
        f"активных месяцев: {prof['active_months']}",
        f"- за последние 14 дней выгрузки: {prof['last14']} сообщений, "
        f"за 14 дней до этого: {prof['prev14']}",
        "",
        "## Как отобран материал",
        "",
        f"Сообщения, написанные подряд, склеены в одну реплику: "
        f"{report['messages_total']} сообщений → {report['replies_total']} реплик.",
        f"Из них показано {report['picked']}"
        + (f" за период {report['covers'][0]} — {report['covers'][1]}" if report["covers"] else "")
        + ". Отбор: голосовые и кружки, самые содержательные реплики, последние недели "
          "и образцы коротких реплик по всему периоду — чтобы была видна и суть, и манера речи.",
    ]
    if report["dropped"]:
        marks = ", ".join(f"{m} — {n}" for m, n in report["dropped_marks"][:6])
        head.append(
            f"Не показано {report['dropped']} реплик ({report['dropped_chars']} знаков); "
            f"по формальным признакам это: {marks}.")
        if report["common_phrases"]:
            often = ", ".join(f"«{t}» ×{n}" for t, n in report["common_phrases"] if n > 1)
            if often:
                head.append(f"Среди непоказанного чаще всего повторялось: {often}.")
    head += [
        "",
        "В квадратных скобках перед репликой: время, вид сообщения, сколько сообщений "
        "склеено, формальные признаки и сколько человек ответили на неё в чате. "
        "Число ответов — мера того, насколько реплика зацепила других.",
        "",
        "## Высказывания",
        "",
    ]
    stats = signal._corpus_stats()
    body = [signal.render(r, stats) for r in picked]
    material = "\n".join(head + body)
    return material, len(picked), prof


def build_prompt(tg_id: int, kind: str = "ментору") -> tuple[str, int, int, dict[str, Any]]:
    material, used, prof = _material(tg_id)
    head = KINDS.get(kind, INSTRUCTION)
    return f"{head}\n\n---\n\n{material}\n", used, len(material), prof


# ----------------------------------------------------------------- очередь

def request(conn: sqlite3.Connection, tg_id: int, user: sqlite3.Row,
            kind: str = "ментору") -> int:
    """Поставить разбор в очередь и сохранить промпт на диск."""
    if kind not in KINDS:
        kind = "ментору"
    prompt, used, chars, prof = build_prompt(tg_id, kind)

    PROMPT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = PROMPT_DIR / f"{tg_id}-{kind}-{stamp}.md"
    path.write_text(prompt, encoding="utf-8")

    cur = conn.execute(
        "INSERT INTO analyses (tg_user_id, status, provider, requested_by, "
        "requested_login, messages_used, material_chars, prompt_path, kind) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (tg_id, "ожидает", PROVIDER, user["id"], user["login"], used, chars,
         str(path), kind))
    request_id = int(cur.lastrowid)

    if PROVIDER == "cli":
        # Отдельным потоком: разбор идёт минуту с лишним, страница ждать
        # не должна. Пока он не готов, карточка показывает «ожидает».
        threading.Thread(target=_cli_worker, args=(request_id,),
                         daemon=True).start()
    return request_id


def _cli_worker(request_id: int) -> None:
    """Своё соединение: sqlite не пускает чужое между потоками."""
    own = store.connect()
    try:
        run_cli(own, request_id)
    except Exception as exc:                       # поток не должен падать молча
        fail(own, request_id, f"сбой движка: {exc}")
    finally:
        own.close()


def latest(conn: sqlite3.Connection, tg_id: int,
           kind: str = "ментору") -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM analyses WHERE tg_user_id=? AND kind=? ORDER BY id DESC LIMIT 1",
        (tg_id, kind)).fetchone()


def pending(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM analyses WHERE status='ожидает' ORDER BY id").fetchall()


def get(conn: sqlite3.Connection, request_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM analyses WHERE id=?", (request_id,)).fetchone()


def save(conn: sqlite3.Connection, request_id: int, text: str) -> None:
    conn.execute(
        "UPDATE analyses SET status='готов', text=?, error=NULL, "
        "finished_at=datetime('now') WHERE id=?", (text.strip(), request_id))


def fail(conn: sqlite3.Connection, request_id: int, error: str) -> None:
    conn.execute(
        "UPDATE analyses SET status='ошибка', error=?, finished_at=datetime('now') "
        "WHERE id=?", (error[:2000], request_id))


# ----------------------------------------------------------------- движки

def run_cli(conn: sqlite3.Connection, request_id: int, timeout: int = 900) -> bool:
    """Отдать промпт установленному Claude Code и забрать ответ.

    Работает на подписке: флаг --bare здесь нельзя, он по устройству читает
    только ANTHROPIC_API_KEY и на вход по подписке отвечает «Not logged in».
    """
    row = get(conn, request_id)
    if row is None:
        return False
    prompt = Path(row["prompt_path"]).read_text(encoding="utf-8")

    try:
        done = subprocess.run(
            ["claude", "-p"], input=prompt, capture_output=True,
            text=True, timeout=timeout, env=claude_env.env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        fail(conn, request_id, f"не удалось запустить claude: {exc}")
        return False

    out = (done.stdout or "").strip()
    if done.returncode != 0 or not out or "Not logged in" in out:
        fail(conn, request_id,
             claude_env.explain((done.stderr or out or "пустой ответ").strip()[:2000]))
        return False
    save(conn, request_id, out)
    return True


PROVIDERS = {
    # Разбор делает Claude Code вручную через analyze.py — движок по умолчанию.
    "queue": None,
    # Тот же Claude Code, но запускается приложением. Нужен залогиненный CLI.
    "cli": run_cli,
    # Место для будущей нейронки: функция (conn, request_id) -> bool,
    # которая кладёт текст через save() или ошибку через fail().
}


# ----------------------------------------------------------------- достаточность

# Порог набран по корпусу ММ5 и ММ6: на этих значениях портрет опирается
# на поведение, а не на догадки. Ниже — текст выходит вежливым пересказом
# трёх реплик, и это хуже, чем его отсутствие.
ENOUGH = {"solid": 40, "words": 2000, "days": 21}
THIN = {"solid": 15, "words": 700}


def readiness(tg_id: int) -> dict[str, Any]:
    """Хватает ли материала на портрет — и если нет, чего именно не хватает.

    Результат кладётся в кэш корпуса: список участников спрашивает его
    по каждому человеку, а пересчитывать склейку реплик на каждый показ
    страницы — лишняя секунда на ровном месте. Кэш сбрасывается сам,
    когда база знаний обновилась.
    """
    cache = corpus._data().setdefault("_readiness", {})
    if tg_id in cache:
        return cache[tg_id]
    verdict = _readiness(tg_id)
    cache[tg_id] = verdict
    return verdict


def _readiness(tg_id: int) -> dict[str, Any]:
    reps = signal.replies_of(tg_id)
    if not reps:
        return {"level": "мало", "solid": 0, "words": 0, "days": 0, "voice": 0,
                "replies": 0, "reason": "участник ничего не писал в собранных чатах"}

    words = sum(len(signal.WORD.findall(r["text"])) for r in reps)
    solid = sum(1 for r in reps
                if ("голос" in r["marks"])
                or (not (r["marks"] & {"шум", "ритуал", "реакция"})
                    and len(signal.WORD.findall(r["text"])) >= 12))
    days = (max(r["when"] for r in reps) - min(r["when"] for r in reps)).days
    voice = sum(1 for r in reps if "голос" in r["marks"])

    data = {"solid": solid, "words": words, "days": days, "voice": voice,
            "replies": len(reps)}

    if solid >= ENOUGH["solid"] and words >= ENOUGH["words"] and days >= ENOUGH["days"]:
        return {**data, "level": "достаточно",
                "reason": f"{solid} содержательных реплик за {days} дней участия"}

    if solid >= THIN["solid"] and words >= THIN["words"]:
        return {**data, "level": "скудно",
                "reason": f"всего {solid} содержательных реплик"
                          + (f" и {days} дней участия" if days < ENOUGH["days"] else "")
                          + " — портрет выйдет коротким и осторожным"}

    missing = []
    if solid < THIN["solid"]:
        missing.append(f"содержательных реплик {solid} из {THIN['solid']}")
    if words < THIN["words"]:
        missing.append(f"слов {words} из {THIN['words']}")
    return {**data, "level": "мало",
            "reason": "материала не хватает: " + ", ".join(missing)}


# ----------------------------------------------------------------- показ

_BOLD = re.compile(r"\*\*(.+?)\*\*")


def to_html(text: str) -> str:
    """Очень маленький markdown: заголовки, списки, жирный, абзацы.

    Библиотека ради пяти правил не нужна, а текст модели сначала экранируется —
    в приложение не должно попадать ничего исполняемого. Перенос строки внутри
    абзаца остаётся переносом: абзац кончается пустой строкой, как в markdown.
    """
    out: list[str] = []
    para: list[str] = []
    in_list = False

    def close_para() -> None:
        if para:
            out.append("<p>" + _BOLD.sub(r"<b>\1</b>", " ".join(para)) + "</p>")
            para.clear()

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for raw in html.escape(text).splitlines():
        line = raw.strip()
        # Перенесённая строка внутри пункта списка начинается с отступа —
        # она продолжает пункт, а не открывает новый абзац.
        if in_list and line and raw[:1].isspace() and not re.match(
                r"^(?:[-—*]\s+|\d+\.\s+)", line):
            out[-1] = out[-1][:-len("</li>")] + " " + _BOLD.sub(r"<b>\1</b>", line) + "</li>"
            continue
        if not line:
            close_para()
            close_list()
            continue
        if line.startswith(("#", "##", "###")) and line.lstrip("#").startswith(" "):
            close_para()
            close_list()
            body = _BOLD.sub(r"<b>\1</b>", line.lstrip("#").strip())
            out.append(f"<h4>{body}</h4>" if line.startswith("###") else f"<h3>{body}</h3>")
        elif re.match(r"^(?:[-—*]\s+|\d+\.\s+)", line):
            close_para()
            item = re.sub(r"^(?:[-—*]\s+|\d+\.\s+)", "", line)
            if not in_list:
                out.append("<ul>")
                in_list = True
            body = _BOLD.sub(r"<b>\1</b>", item)
            out.append(f"<li>{body}</li>")
        else:
            close_list()
            para.append(line)
    close_para()
    close_list()
    return "\n".join(out)
