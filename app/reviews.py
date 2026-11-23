"""Разборы участника по записям и его цели.

Разбор — это сессия, где менторы вместе с участником копают его бизнес
и жизнь и ставят цели на поток. Запись разбора (звук или готовая
расшифровка) загружается в карточку, приложение расшифровывает её
движком Стенографа, а Claude Code пишет выжимку для ментора: что
выкопали, где сопротивление, что вдохновит, куда не докопали — и
предлагает цели. Цели ложатся отдельными строками: дальше по ним
ментор отмечает, идёт человек или буксует.

Тот же порядок, что у разборов по переписке (analysis.py): запрос
сохраняется, работа идёт отдельным потоком, карточка показывает статус.
"""

from __future__ import annotations

import os
import re
import subprocess
import json
import threading
from pathlib import Path
from typing import Any

from . import claude_env, store
from .config import APP_DB

# Записи и расшифровки разборов — рядом с базой приложения, не в репозитории:
# это личные разговоры участников.
REVIEW_DIR = Path(os.environ.get("VERSHINA_REVIEW_DIR", APP_DB.parent / "reviews"))

# Расшифровка — движок Стенографа (GigaAM + разделение голосов по тембру).
STT_PYTHON = os.environ.get("VERSHINA_STT_PYTHON",
                            "/opt/vershina/venvs/stenograf-stt/bin/python")
STT_SCRIPT = os.environ.get("VERSHINA_STT_SCRIPT",
                            "/opt/vershina/projects/stenograf/stt_speakers.py")

AUDIO_EXT = {".m4a", ".mp3", ".wav", ".ogg", ".oga", ".opus", ".webm", ".mp4", ".mkv", ".aac", ".flac"}
TEXT_EXT = {".txt", ".md"}

# Сколько расшифровки отдаём модели: двухчасовой разбор — около 120 тыс. знаков,
# это укладывается в одно обращение.
TRANSCRIPT_LIMIT = int(os.environ.get("VERSHINA_REVIEW_LIMIT", 160_000))

GOAL_STATUSES = ["идёт", "буксует", "выполнена", "выпал"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_user_id   INTEGER NOT NULL,
    title        TEXT,
    held_on      TEXT,                 -- дата разбора, ГГГГ-ММ-ДД
    status       TEXT NOT NULL DEFAULT 'ожидает', -- ожидает | расшифровка | выжимка | готов | ошибка
    source_name  TEXT,                 -- имя загруженного файла
    transcript   TEXT,
    summary      TEXT,
    error        TEXT,
    added_by     TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_review_user ON reviews(tg_user_id, held_on);

-- Цели участника на поток. Источник — разбор (review_id) или ментор руками.
CREATE TABLE IF NOT EXISTS goals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_user_id  INTEGER NOT NULL,
    text        TEXT NOT NULL,
    due         TEXT,                  -- срок как его сформулировали: дата или «к концу потока»
    status      TEXT NOT NULL DEFAULT 'идёт',
    note        TEXT,
    review_id   INTEGER,
    updated_by  TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_goal_user ON goals(tg_user_id);

-- Шаги к цели. Отмечает ИИ по отчётам (done_by='по отчёту') или ментор.
CREATE TABLE IF NOT EXISTS goal_steps (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id   INTEGER NOT NULL,
    title     TEXT NOT NULL,
    deadline  TEXT,
    done      INTEGER NOT NULL DEFAULT 0,
    done_by   TEXT,
    evidence  TEXT                    -- что подтверждает: цитата из отчёта
);
CREATE INDEX IF NOT EXISTS idx_gstep_goal ON goal_steps(goal_id);

-- Декларация, собранная из разбора: черновик, пока ментор не подтвердил.
CREATE TABLE IF NOT EXISTS declarations (
    review_id    INTEGER PRIMARY KEY,
    tg_user_id   INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'черновик',   -- черновик | подтверждена
    money_goal   INTEGER,
    word_price   TEXT,
    perk         TEXT,
    confirmed_by TEXT,
    confirmed_at TEXT
);
"""

PROMPT = """Ты помогаешь менторам закрытого бизнес-сообщества «Вершина»
(мастермайнды для предпринимателей). Ниже — расшифровка разбора: сессии,
где менторы и группа вместе с участником копают его бизнес и жизнь,
ищут, что на самом деле мешает, и ставят цели на поток. Расшифровка
автоматическая: возможны ошибки в словах, говорящие подписаны именами
или «Голос N».

Участник, которого разбирали: {name}.

Напиши выжимку для ментора, который будет вести этого человека весь поток
и, возможно, не был на разборе. Структура строго такая:

## Кратко о человеке
Два-три предложения: чем занимается, масштаб, ситуация, с каким запросом пришёл.

## Что выкопали
Главное, до чего дошли на разборе: настоящий запрос под заявленным,
ключевые ограничения, повороты разговора.

## Где сопротивление
Где человек закрывался, спорил, уходил от темы, оправдывался.

## Что будет вдохновлять
На что он загорался, какие формулировки целей и смыслы его цепляют.

## Куда не докопали
Темы, которые всплыли, но не разобраны; вопросы, которые стоило задать.

## Как вести дальше
3–5 пунктов ментору: на что давить, как поддерживать, чего избегать.

## Цифры
Все цифры, которые прозвучали про деньги и масштаб (доход, чистая прибыль,
оборот, расходы, долги, сотрудники, цели в деньгах), — таблицей:
| Что | Сколько | За какой период | Кто сказал | Расхождение с анкетой |
Период пиши только если он прозвучал («в месяц», «в год»); не прозвучал — «не назван».
«Кто сказал» — сам участник или группа/ментор (их догадки и расчёты — не его цифры).
Расхождение — если в справке ниже цифра другая; иначе «—».

## Цена слова, плюшка, обязательства
Цена слова и плюшка — если звучали, дословно; не звучали — так и напиши.
Обязательства: кредиты, ипотеки, долги, партнёрские обязательства, банкротство —
что прозвучало, с суммами; не прозвучало — «не прозвучало».

## Откуда цели
Каждая цель из декларации ниже — одной строкой: чьи это слова (его / группы /
ментора), согласился ли он («да», «частично», «торговался», «не прозвучало»)
и короткая дословная цитата.

## Цели
Цели, которые поставили или стоит поставить по итогам разбора. Каждая
отдельной строкой ровно в формате:
- формулировка цели | срок
Срок — как прозвучал («к концу потока», «до 1 декабря»); если не звучал — «—».

Жёсткие правила:
— Только то, что есть в расшифровке. Не выдумывай факты и цифры; если
  чего-то не видно — так и пиши.
— Никаких диагнозов и клинических ярлыков: пишешь про поведение и задачи.
— Без вступления и заключения, только разделы выше.

Расшифровка:
{transcript}
"""


DECL_PROMPT = """Ниже расшифровка разбора участника закрытого бизнес-сообщества
«Вершина». В конце разбора участник вместе с менторами формирует декларацию
на поток: цели, шаги к ним и сроки. Участник: {name}. Сегодня {today}.

Собери декларацию строго по тому, что прозвучало. Ответ — только JSON
без пояснений и без markdown, ровно такой структуры:

{{"goals": [
   {{"title": "цель как сформулировали",
     "deadline": "ГГГГ-ММ-ДД или null",
     "proof": "чем участник подтвердит результат, или null",
     "steps": [{{"title": "шаг", "deadline": "ГГГГ-ММ-ДД или null"}}]}}
 ],
 "money_goal": число в рублях или null,
 "word_price": "цена слова, как прозвучала, или null",
 "perk": "плюшка-награда, как прозвучала, или null",
 "facts": {{"city": факт, "work": факт, "income": факт, "request": факт, "about": факт}}}}

«facts» — сведения о человеке для карточки, каждое {{"value": "коротко", "quote":
"дословная фраза из расшифровки"}} или null, если не звучало: city — где живёт,
work — чем занимается, income — доход как назвал, request — запрос на поток своими
словами, about — 1–2 предложения: возраст, семья, главное о нём. Только сказанное
самим участником о себе, не догадки группы; только устойчивое, не события дня;
только новое или отличное от справки выше (совпадает по смыслу — null).

Правила: только то, что есть в расшифровке; срок вида «до конца потока»
переведи в дату, только если дата конца потока прозвучала, иначе null;
если декларацию на разборе не формировали — верни {{"goals": []}}.

Расшифровка:
{transcript}
"""


# 4-дневный мастермайнд: разбор — день 2, цели и декларация — день 3. На разборе
# декларацию ещё не собирают, поэтому просим черновик целей по трём сферам из того,
# что выкопали, — на третий день его доводят до декларации и подписывают.
DECL_PROMPT_4D = """Ниже расшифровка разбора участника 4-дневного мастермайнда «Вершина».
Завтра участник ставит цели на основной поток (2 месяца) по трём сферам: бизнес,
личное, семья/отношения — с точной формулировкой, цифрой, сроком и шагами.
Участник: {name}. Сегодня {today}.

Собери ЧЕРНОВИК целей из того, что прозвучало на разборе. Цели ВЫБИРАЙ из сказанного,
а не придумывай: бери то, что назвал сам участник, или то, что предложила группа и
он с этим согласился. Если он прямо отказался («не согласен», «нет») — такую цель
не включай. Цифру и период бери ровно как прозвучали: не превращай «в месяц» в «в год»
и наоборот; если период не прозвучал — не дописывай его. Формулировка как результат
в прошедшем времени («Вышел на 3 млн чистой прибыли в месяц»); 0–3 цели на сферу —
сферу без сказанного оставь пустой; по 2–4 шага к каждой, шаги тоже из разбора. Название цели начинается строго с одной из трёх сфер:
«Бизнес: », «Личное: » (здоровье, спорт, саморазвитие, эмоции — сюда) или «Семья: »
(отношения, дети, родители) — других сфер не заводи. Не выдумывай фактов о человеке. Ответ — только JSON без
пояснений и markdown:

{{"goals": [
   {{"title": "Бизнес: …", "deadline": null, "proof": "чем подтвердит, или null",
     "steps": [{{"title": "шаг", "deadline": null}}]}}
 ],
 "money_goal": число в рублях, если звучала цель в деньгах, иначе null,
 "word_price": "цена слова, как прозвучала, или null",
 "perk": "плюшка-награда, как прозвучала, или null",
 "facts": {{"city": факт, "work": факт, "income": факт, "request": факт, "about": факт}}}}

«facts» — сведения о человеке для карточки, каждое {{"value": "коротко", "quote":
"дословная фраза из расшифровки"}} или null, если не звучало: city — где живёт,
work — чем занимается, income — доход как назвал, request — запрос на поток своими
словами, about — 1–2 предложения: возраст, семья, главное о нём. Только сказанное
самим участником о себе, не догадки группы; только устойчивое, не события дня;
только новое или отличное от справки выше (совпадает по смыслу — null).

Расшифровка:
{transcript}
"""


SPLIT = "===JSON==="

CONTEXT_BLOCK = """
Что уже известно об участнике (анкета, карточка, прошлые разборы и созвоны).
Опирайся на это: сверяй с разбором, расхождения отмечай прямо («в анкете — …, на
разборе — …»), известное не выдавай за открытие. Если справка пустая — участник новый.
---
{context}
---
"""


def combined_prompt(name: str, today: str, transcript: str, context: str, fourday: bool) -> str:
    """Выжимка, декларация и сведения — одним запросом: расшифровка уходит в Claude
    один раз, а не три (так втрое дешевле по токенам, 18.10.2026)."""
    head = PROMPT.split("Расшифровка:")[0].format(name=name)
    # цели здесь — в JSON ниже, раздел «## Цели» в выжимке был бы повтором
    head = re.sub(r"## Цели\n.*?(?=Жёсткие правила)", "", head, flags=re.S)
    head = head.replace("Без вступления и заключения, только разделы выше.",
                        "Без вступления и заключения: разделы выше, затем декларация.")
    decl = (DECL_PROMPT_4D if fourday else DECL_PROMPT).split("Расшифровка:")[0]
    decl = decl.format(name=name, today=today, transcript="")
    decl = decl[decl.index("Участник:"):] if "Участник:" in decl else decl
    decl = decl.replace("Ответ — только JSON", "Эта часть — только JSON")
    return (head + CONTEXT_BLOCK.format(context=context or "(пусто)")
            + f"\nПосле выжимки — строка {SPLIT} и за ней декларация.\n" + decl
            + "\nРасшифровка:\n" + transcript)


def split_answer(raw: str) -> tuple[str, str]:
    """Ответ единого запроса → (выжимка, JSON-часть)."""
    if SPLIT in raw:
        a, b = raw.split(SPLIT, 1)
        return a.strip(), b.strip()
    m = re.search(r"\{\s*\"goals\".*\}", raw, flags=re.S)       # строку-разделитель забыли
    if m:
        return re.sub(r"[`\s]*(json)?[`\s]*$", "", raw[:m.start()]).strip(), m.group(0)
    return raw.strip(), ""


def init(conn) -> None:
    conn.executescript(SCHEMA)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(goals)")}
    for col, decl in (("proof", "TEXT"), ("deadline", "TEXT")):
        if col not in cols:
            conn.execute(f"ALTER TABLE goals ADD COLUMN {col} {decl}")


# ----------------------------------------------------------------- данные

def of(conn, tg_id: int) -> list:
    return conn.execute(
        "SELECT * FROM reviews WHERE tg_user_id=? ORDER BY COALESCE(held_on, created_at) DESC, id DESC",
        (tg_id,)).fetchall()


def get(conn, review_id: int):
    return conn.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()


def goals_of(conn, tg_id: int) -> list:
    return conn.execute(
        "SELECT * FROM goals WHERE tg_user_id=? ORDER BY CASE status "
        "WHEN 'буксует' THEN 0 WHEN 'идёт' THEN 1 WHEN 'выпал' THEN 2 ELSE 3 END, id",
        (tg_id,)).fetchall()


def add_goal(conn, tg_id: int, text: str, due: str, who: str, review_id: int | None = None) -> None:
    conn.execute(
        "INSERT INTO goals (tg_user_id, text, due, review_id, updated_by) VALUES (?,?,?,?,?)",
        (tg_id, text.strip()[:500], (due or "").strip()[:80] or None, review_id, who))


def set_goal(conn, goal_id: int, status: str, note: str, who: str) -> None:
    if status not in GOAL_STATUSES:
        return
    conn.execute(
        "UPDATE goals SET status=?, note=?, updated_by=?, updated_at=datetime('now') WHERE id=?",
        (status, (note or "").strip()[:500] or None, who, goal_id))


def remove_goal(conn, goal_id: int) -> None:
    conn.execute("DELETE FROM goals WHERE id=?", (goal_id,))


def create(conn, tg_id: int, title: str, held_on: str, filename: str,
           data: bytes, who: str) -> int:
    """Сохранить файл разбора и запустить обработку в отдельном потоке."""
    ext = Path(filename or "").suffix.lower()
    if ext not in AUDIO_EXT | TEXT_EXT:
        raise ValueError("Нужна запись (m4a, mp3, wav, ogg, webm, mp4) или расшифровка в .txt")
    cur = conn.execute(
        "INSERT INTO reviews (tg_user_id, title, held_on, source_name, added_by) VALUES (?,?,?,?,?)",
        (tg_id, (title or "").strip()[:200] or "Разбор", held_on or None, filename, who))
    rid = int(cur.lastrowid)
    folder = REVIEW_DIR / str(rid)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"source{ext}").write_bytes(data)
    threading.Thread(target=_worker, args=(rid,), daemon=True).start()
    return rid


def create_from_file(conn, tg_id: int, title: str, held_on: str | None, path: Path,
                     source_name: str, who: str) -> int:
    """То же, что create, но файл уже на диске (записи по 2–3 ГБ не держим в
    памяти) — переносится в папку разбора."""
    ext = Path(source_name).suffix.lower()
    if ext not in AUDIO_EXT | TEXT_EXT:
        raise ValueError(f"не запись: {source_name}")
    cur = conn.execute(
        "INSERT INTO reviews (tg_user_id, title, held_on, source_name, added_by) VALUES (?,?,?,?,?)",
        (tg_id, (title or "").strip()[:200] or "Разбор", held_on, source_name, who))
    rid = int(cur.lastrowid)
    folder = REVIEW_DIR / str(rid)
    folder.mkdir(parents=True, exist_ok=True)
    Path(path).replace(folder / f"source{ext}")
    threading.Thread(target=_worker, args=(rid,), daemon=True).start()
    return rid


def drop_source(review_id: int) -> None:
    """Исходная запись больше не нужна: расшифровка в базе."""
    src = _source(review_id)
    if src and src.suffix.lower() in AUDIO_EXT:
        src.unlink(missing_ok=True)


def resume() -> None:
    """Разборы, оборванные перезапуском контейнера, — продолжить (вызывается
    одним процессом при старте)."""
    with store.connect() as conn:
        rows = conn.execute("SELECT id FROM reviews WHERE status IN "
                            "('ожидает','расшифровка','выжимка','декларация')").fetchall()
    for r in rows:
        threading.Thread(target=_worker, args=(r["id"],), daemon=True).start()


def retry(conn, review_id: int) -> None:
    conn.execute("UPDATE reviews SET status='ожидает', error=NULL WHERE id=?", (review_id,))
    threading.Thread(target=_worker, args=(review_id,), daemon=True).start()


def remove(conn, review_id: int) -> None:
    gids = [r["id"] for r in conn.execute("SELECT id FROM goals WHERE review_id=?", (review_id,))]
    if gids:
        conn.execute(f"DELETE FROM goal_steps WHERE goal_id IN ({','.join('?'*len(gids))})", gids)
    conn.execute("DELETE FROM declarations WHERE review_id=?", (review_id,))
    conn.execute("DELETE FROM goals WHERE review_id=?", (review_id,))
    conn.execute("DELETE FROM reviews WHERE id=?", (review_id,))
    folder = REVIEW_DIR / str(review_id)
    if folder.exists():
        for p in folder.iterdir():
            p.unlink()
        folder.rmdir()


# ----------------------------------------------------------------- обработка

def _set(conn, rid: int, **kw) -> None:
    cols = ", ".join(f"{k}=?" for k in kw)
    conn.execute(f"UPDATE reviews SET {cols} WHERE id=?", (*kw.values(), rid))


def _worker(rid: int) -> None:
    own = store.connect()
    try:
        _process(own, rid)
    except Exception as exc:                   # поток не должен падать молча
        _set(own, rid, status="ошибка", error=f"сбой обработки: {exc}"[:2000],
             finished_at=_now())
    finally:
        own.close()


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _source(rid: int) -> Path | None:
    folder = REVIEW_DIR / str(rid)
    found = sorted(folder.glob("source.*")) if folder.exists() else []
    return found[0] if found else None


def _process(conn, rid: int) -> None:
    row = get(conn, rid)
    if row is None:
        return
    src = _source(rid)
    transcript = row["transcript"]
    if src is None and not transcript:
        _set(conn, rid, status="ошибка", error="файл разбора пропал", finished_at=_now())
        return

    if not transcript:
        if src.suffix.lower() in TEXT_EXT:
            transcript = src.read_text(encoding="utf-8", errors="replace")
        else:
            transcript = _transcribe(src, row["title"] or f"разбор {rid}",
                                     lambda pos: _set(conn, rid, status="ожидает",
                                                      error=f"{pos}-й в очереди сервера на расшифровку"),
                                     lambda: _set(conn, rid, status="расшифровка", error=None))
        _set(conn, rid, transcript=transcript)

    _set(conn, rid, status="выжимка")
    from . import corpus
    prof = corpus.profile_of(row["tg_user_id"]) or {}
    name = prof.get("name")
    if not name and row["tg_user_id"] < 0:            # участник потока на платформе — имя из карточки
        p = conn.execute("SELECT name FROM people WHERE id=?", (-row["tg_user_id"],)).fetchone()
        name = " ".join(reversed(p["name"].split())) if p and len(p["name"].split()) == 2 else (p["name"] if p else None)
    name = name or f"участник {row['tg_user_id']}"
    text = transcript[:TRANSCRIPT_LIMIT]
    from datetime import date as _date
    from . import context as _ctx
    fourday = False
    if row["tg_user_id"] < 0:
        from . import flows as _flows, fourday as _fd
        pf = conn.execute("SELECT flow FROM people WHERE id=?", (-row["tg_user_id"],)).fetchone()
        fourday = bool(pf and _fd.is_fourday(_flows.get(conn, name=pf["flow"])))
    try:
        known = _ctx.person(conn, row["tg_user_id"], skip_review=rid)
    except Exception:
        known = ""
    raw = _ask_claude(combined_prompt(name, _date.today().isoformat(), text, known, fourday))
    summary, tail = split_answer(raw)
    _set(conn, rid, summary=summary, status="декларация", error=None)
    decl = parse_declaration(tail) if tail else None
    save_declaration(conn, row["tg_user_id"], rid, decl, summary)
    if decl:
        from . import person_info
        try:
            person_info.propose(conn, row["tg_user_id"], decl.get("facts"),
                                f"разбор {row['held_on'] or ''}".strip(), str(rid))
        except Exception:
            pass
    if decl and row["tg_user_id"] < 0:                 # карточка на платформе — черновик прямо в декларацию
        from . import decl as _decl
        try:
            _decl.from_review(conn, -row["tg_user_id"], decl)
        except Exception:
            pass
    _set(conn, rid, status="готов", finished_at=_now())


def _slot(title: str, on_wait):
    """Место в общей очереди сервера (heavy.py Стенографа): встречи Стенографа
    идут первыми, две расшифровки не съедают память друг у друга. Без
    Стенографа рядом (локальный запуск) — сразу и с потоками по умолчанию."""
    import contextlib
    import sys
    sys.path.insert(0, str(Path(STT_SCRIPT).parent))
    try:
        import heavy
    except ImportError:
        return contextlib.nullcontext({})
    return heavy.slot("платформа", title, priority=1, on_wait=on_wait)


def _transcribe(src: Path, title: str = "", on_wait=None, on_start=None) -> str:
    """Расшифровка движком Стенографа: реплики с подписями «Имя: …»."""
    with _slot(title, on_wait) as threads:
        if on_start:
            on_start()
        # низший приоритет: веб-страницы платформы важнее
        r = subprocess.run([STT_PYTHON, STT_SCRIPT, str(src), str(src.with_name("speakers.json"))],
                           capture_output=True, text=True, timeout=4 * 3600,
                           env={**os.environ, **threads}, preexec_fn=lambda: os.nice(19))
    (src.with_name("stt.log")).write_text(r.stderr or "")
    raw = (r.stdout or "").strip()
    try:
        res = json.loads(raw.splitlines()[-1]) if raw else {}
    except ValueError:
        res = {}
    text = (res.get("labeled") or res.get("text") or "").strip()
    if not text:
        raise RuntimeError("расшифровка пустая: " + (r.stderr or "")[-300:])
    return text


def _ask_claude(prompt: str, timeout: int = 1200) -> str:
    """Claude Code на подписке, как в разборах по переписке (analysis.run_cli)."""
    done = subprocess.run(["claude", "-p"], input=prompt, capture_output=True,
                          text=True, timeout=timeout, env=claude_env.env())
    out = (done.stdout or "").strip()
    if done.returncode != 0 or not out or "Not logged in" in out:
        raise RuntimeError(claude_env.explain((done.stderr or out or "пустой ответ").strip()[:500]))
    return out


def parse_declaration(raw: str) -> dict | None:
    """JSON декларации из ответа модели; терпим обёртку ```json."""
    m = re.search(r"\{.*\}", raw or "", flags=re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return None
    return d if isinstance(d.get("goals"), list) else None


def _date_or_none(v) -> str | None:
    return v if isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) else None


def save_declaration(conn, tg_id: int, rid: int, decl: dict | None, summary: str) -> None:
    """Записать цели и шаги разбора. Нет JSON — берём список целей из выжимки."""
    old = [r["id"] for r in conn.execute("SELECT id FROM goals WHERE review_id=?", (rid,))]
    if old:
        conn.execute(f"DELETE FROM goal_steps WHERE goal_id IN ({','.join('?'*len(old))})", old)
    conn.execute("DELETE FROM goals WHERE review_id=?", (rid,))
    conn.execute("DELETE FROM declarations WHERE review_id=?", (rid,))
    if decl is None:
        for goal, due in parse_goals(summary):
            add_goal(conn, tg_id, goal, "" if due == "—" else due, "из разбора", rid)
        return
    money = decl.get("money_goal")
    conn.execute("INSERT INTO declarations (review_id, tg_user_id, money_goal, word_price, perk) "
                 "VALUES (?,?,?,?,?)",
                 (rid, tg_id, int(money) if isinstance(money, (int, float)) else None,
                  decl.get("word_price") or None, decl.get("perk") or None))
    for g in decl["goals"][:25]:
        title = str(g.get("title") or "").strip()
        if not title:
            continue
        deadline = _date_or_none(g.get("deadline"))
        cur = conn.execute(
            "INSERT INTO goals (tg_user_id, text, due, deadline, proof, review_id, updated_by) "
            "VALUES (?,?,?,?,?,?, 'из разбора')",
            (tg_id, title[:500], deadline, deadline, (g.get("proof") or None), rid))
        gid = cur.lastrowid
        for st in (g.get("steps") or [])[:40]:
            t = str(st.get("title") or "").strip()
            if t:
                conn.execute("INSERT INTO goal_steps (goal_id, title, deadline) VALUES (?,?,?)",
                             (gid, t[:300], _date_or_none(st.get("deadline"))))


def declaration_of(conn, rid: int):
    return conn.execute("SELECT * FROM declarations WHERE review_id=?", (rid,)).fetchone()


def confirm_declaration(conn, rid: int, who: str) -> None:
    conn.execute("UPDATE declarations SET status='подтверждена', confirmed_by=?, "
                 "confirmed_at=datetime('now') WHERE review_id=?", (who, rid))


def steps_of(conn, goal_ids: list[int]) -> dict[int, list]:
    out: dict[int, list] = {}
    if not goal_ids:
        return out
    for s in conn.execute(
            f"SELECT * FROM goal_steps WHERE goal_id IN ({','.join('?'*len(goal_ids))}) "
            "ORDER BY COALESCE(deadline,'9999'), id", goal_ids):
        out.setdefault(s["goal_id"], []).append(s)
    return out


def toggle_step(conn, step_id: int, who: str) -> None:
    conn.execute("UPDATE goal_steps SET done = 1 - done, done_by=? WHERE id=?", (who, step_id))


GOAL_LINE = re.compile(r"^\s*[-•*]\s*(.+?)\s*\|\s*(.+?)\s*$")


def parse_goals(summary: str) -> list[tuple[str, str]]:
    """Строки «- цель | срок» из раздела «## Цели»."""
    part = re.split(r"^##\s*Цели\s*$", summary or "", flags=re.M)
    if len(part) < 2:
        return []
    out = []
    for line in part[1].splitlines():
        if line.startswith("## "):
            break
        m = GOAL_LINE.match(line)
        if m:
            out.append((m.group(1).strip(), m.group(2).strip()))
    return out[:12]


def summary_without_goals(summary: str) -> str:
    """Выжимка без раздела целей: цели в карточке показываются своим блоком."""
    return re.split(r"^##\s*Цели\s*$", summary or "", flags=re.M)[0].strip()
