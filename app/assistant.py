"""Инструменты помощника: то, что он умеет узнавать о сообществе.

Каждая функция — один вопрос к данным и один короткий ответ обычным текстом.
Их вызывает либо модель (движок `api`), либо простой разборщик вопросов
(движок `rules`) — поэтому здесь нет ни одной строчки про нейронку.

Данные берутся оттуда же, откуда их берёт веб-приложение: база знаний
только на чтение, база приложения — вовлечения, группы, разборы, цели.
"""

from __future__ import annotations

import difflib
import os
import re
from typing import Any

from . import analysis, corpus, ledger, store

# Выдержки из переписки в ответах помощника. Он живёт у организаторов,
# поэтому по умолчанию цитаты разрешены; участникам этот канал не даётся.
QUOTES = os.environ.get("VERSHINA_BOT_QUOTES", "1") != "0"

LIMIT = 4000       # ответ инструмента не должен съедать контекст целиком


# ------------------------------------------------------------------ мелочи

def _money(n: int | None) -> str:
    n = int(n or 0)
    s = f"{abs(n):,}".replace(",", " ")
    return f"−{s} ₽" if n < 0 else f"{s} ₽"


def _date(raw: str | None) -> str:
    if not raw:
        return "—"
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(raw))
    return f"{m[3]}.{m[2]}.{m[1]}" if m else str(raw)[:10]


def _cut(text: str, limit: int = LIMIT) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "\n…(обрезано)"


# --------------------------------------------------------- поиск человека

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "i", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def _skeleton(text: str) -> str:
    """Имя → латинский каркас. В выгрузке Telegram Desktop половина людей
    записана латиницей («Ivan Petrov»), а спрашивают про них по-русски."""
    out = []
    for ch in (text or "").lower():
        out.append(_TRANSLIT.get(ch, ch if ch.isalnum() else " "))
    s = "".join(out)
    s = s.replace("kh", "h").replace("yu", "u").replace("ya", "a")
    s = s.replace("j", "i").replace("y", "i").replace("ii", "i")
    return " ".join(s.split())


def _people() -> list[dict[str, Any]]:
    return corpus.participants()


def resolve(who: str, loose: bool = True) -> list[dict[str, Any]]:
    """Имя, часть имени, @логин или числовой id → список подходящих людей.

    loose=False выключает самую слабую ступень — совпадение по началу слова.
    Она нужна, когда человека называют в падеже вживую, и мешает, когда по
    тексту ищут имена автоматически: «Так держать» не должно превращаться
    в участника по фамилии Такташев.
    """
    who = (who or "").strip().lstrip("@")
    if not who:
        return []
    people = _people()
    if who.isdigit():
        return [p for p in people if p["user_id"] == int(who)]

    need = _skeleton(who)
    parts = [w for w in need.split() if len(w) > 2]
    exact, fuzzy, stems = [], [], []
    for p in people:
        skel = _skeleton(f"{p['name']} {p.get('username') or ''}")
        words = skel.split()
        if need and need in skel:
            exact.append(p)
        elif parts and all(any(w.startswith(q) for w in words) for q in parts):
            exact.append(p)
        elif parts and any(
                difflib.SequenceMatcher(None, q, w).ratio() >= 0.78
                for q in parts for w in words if len(w) > 2):
            fuzzy.append(p)
        # Последняя попытка — по началу слова: русское имя в вопросе стоит
        # в падеже («по Марье»), а в базе лежит в именительном.
        elif parts and any(
                q[:3] == w[:3]
                and difflib.SequenceMatcher(None, q, w).ratio() >= 0.6
                for q in parts for w in words if len(q) >= 4 and len(w) >= 4):
            stems.append(p)
    return exact or fuzzy or (stems if loose else [])


def _one(who: str) -> tuple[dict[str, Any] | None, str]:
    found = resolve(who)
    if not found:
        return None, (f"Человек «{who}» в базе не найден. "
                      "Проверьте написание или спросите список участников.")
    if len(found) > 1:
        names = ", ".join(f"{p['name']} (id {p['user_id']})" for p in found[:8])
        return None, (f"По запросу «{who}» подходит несколько человек: {names}. "
                      "Уточните, о ком речь.")
    return found[0], ""


# ------------------------------------------------------------- инструменты

def find_person(name: str) -> str:
    """Найти участника по имени, фамилии или логину. Возвращает id, статус
    активности и число сообщений. Годится, когда имя названо неточно."""
    found = resolve(name)
    if not found:
        return f"По запросу «{name}» никого нет. Всего в базе {len(_people())} участников."
    lines = [f"Нашлось {len(found)}:"]
    for p in found[:15]:
        lines.append(f"· {p['name']} — id {p['user_id']}, {p['status']}, "
                     f"{p['messages']} сообщений, последняя активность "
                     f"{_date(p['last_seen'])}")
    return "\n".join(lines)


def person_card(who: str) -> str:
    """Полная карточка человека: активность в чатах, статус тишины,
    вовлечения и деньги из журнала, штрафы, цели, наличие готового разбора."""
    p, err = _one(who)
    if p is None:
        return err
    conn = store.connect()
    tot = ledger.totals_of(conn, p["user_id"])
    team = ledger.team_of(conn, p["user_id"])
    prof = conn.execute("SELECT * FROM participant_profile WHERE tg_user_id=?",
                        (p["user_id"],)).fetchone()
    ment = analysis.latest(conn, p["user_id"], "ментору")
    port = analysis.latest(conn, p["user_id"], "участнику")
    ready = analysis.readiness(p["user_id"])
    conn.close()

    chats = ", ".join(f"{k} — {v}" for k, v in list(p["chats"].items())[:5])
    out = [
        f"{p['name']} (id {p['user_id']})",
        f"Статус: {p['status']}, молчит {p['silent_days']} дн. "
        f"(последняя активность {_date(p['last_seen'])})",
        f"Сообщений: {p['messages']}, из них голосом и кружками "
        f"{p['voice'] + p['circles']}; слов {p['words']}",
        f"В базе с {_date(p['first_seen'])}, активных месяцев {p['active_months']}",
        f"За 14 дней {p['last14']} сообщений, за предыдущие 14 — {p['prev14']} "
        f"(изменение {p['trend']:+d})",
        f"Чаты: {chats or '—'}",
        "",
        f"Вовлечений в журнале: {tot['count']}"
        + (f", принесено {_money(tot['brought'])}, из них оплачено "
           f"{_money(tot['paid'])}" if tot["count"] else ""),
        f"Штрафы и прочее: {_money(tot['charges'])}"
        + (f" (в том числе штрафов {_money(tot['fines'])})" if tot["fines"] else ""),
        f"Итого по деньгам: {_money(tot['net'])}",
        f"Группа: {team['title'] + ' (' + team['flow'] + ')' if team else 'не назначена'}",
    ]
    if tot["count"] == 0:
        out.append("Внимание: вовлечения вносятся в журнал руками. Ноль здесь "
                   "значит «не внесено», а не «не приводил людей» — проверьте "
                   "инструментом involvement_mentions.")
    if prof and (prof["goals"] or prof["about"] or prof["need"]):
        out += ["", "Со слов самого участника:"]
        for k, label in (("goals", "цели на ММ"), ("about", "о себе"),
                         ("need", "нужна помощь")):
            if prof[k]:
                out.append(f"· {label}: {prof[k]}")
    out += ["", f"Материала на разбор: {ready['level']} — {ready['reason']}",
            f"Разбор для ментора: {'есть от ' + _date(ment['finished_at']) if ment and ment['status'] == 'готов' else 'нет'}",
            f"Портрет для участника: {'есть от ' + _date(port['finished_at']) if port and port['status'] == 'готов' else 'нет'}"]
    return _cut("\n".join(out))


def quiet_list(days: int = 7, limit: int = 25) -> str:
    """Кто затих: участники, не писавшие дольше указанного числа дней,
    от самых давно молчащих. Это первый список для работы менторов."""
    people = [p for p in _people() if p["silent_days"] >= max(1, days)]
    people.sort(key=lambda p: -p["silent_days"])
    if not people:
        return f"Молчащих дольше {days} дн. нет."
    conn = store.connect()
    tot = ledger.totals_for(conn, [p["user_id"] for p in people[:limit]])
    conn.close()
    head = (f"Молчат дольше {days} дн.: {len(people)} человек "
            f"(отсчёт от последнего дня в базе — {_date(str(corpus.overview()['last']))})")
    lines = [head]
    for p in people[:limit]:
        t = tot.get(p["user_id"], {})
        lines.append(f"· {p['name']} — {p['silent_days']} дн., {p['status']}, "
                     f"всего {p['messages']} сообщений, вовлечений "
                     f"{t.get('count', 0)}")
    return _cut("\n".join(lines))


def involvements_top(limit: int = 15) -> str:
    """Рейтинг по вовлечениям из журнала: кто сколько привёл и на какие суммы."""
    conn = store.connect()
    rows = conn.execute(
        "SELECT tg_user_id, COUNT(*) n, COALESCE(SUM(amount),0) s "
        "FROM involvements GROUP BY tg_user_id ORDER BY n DESC, s DESC "
        "LIMIT ?", (max(1, limit),)).fetchall()
    flow = ledger.flow_totals(conn)
    conn.close()
    if not rows:
        return ("В журнале вовлечений нет ни одной записи — их вносят руками, "
                "и пока этого не делали. Сами вовлечения в чатах объявлялись: "
                "смотрите involvement_mentions по конкретному человеку.")
    lines = [f"Всего вовлечений {flow['count']} у {flow['people']} человек, "
             f"принесено {_money(flow['brought'])}."]
    for r in rows:
        lines.append(f"· {corpus.name_of(r['tg_user_id'])} — {r['n']} вовлечений, "
                     f"{_money(r['s'])}")
    return _cut("\n".join(lines))


def teams_overview() -> str:
    """Все группы потока: состав, вовлечения, сколько осталось до цели."""
    conn = store.connect()
    rows = ledger.teams(conn, with_archived=False)
    if not rows:
        conn.close()
        return ("Группы ещё не заведены. Их создают в разделе «Группы»: "
                "название, поток, цель по вовлечениям и состав.")
    lines = []
    for t in rows:
        s = ledger.team_summary(conn, t["id"])
        left = f", до цели {s['left']}" if s["goal"] else ""
        lines.append(f"· {t['title']} ({t['flow']}) — {s['members']} человек, "
                     f"{s['count']} вовлечений из {s['goal'] or '—'}{left}, "
                     f"{_money(s['brought'])}")
    conn.close()
    return _cut("Группы:\n" + "\n".join(lines))


def team_card(title: str) -> str:
    """Одна группа целиком: кто в ней, сколько вовлечений у каждого,
    сколько осталось до победы."""
    conn = store.connect()
    rows = ledger.teams(conn, with_archived=True)
    need = _skeleton(title)
    match = [t for t in rows if need and need in _skeleton(t["title"])] or \
            [t for t in rows if title.strip().isdigit() and t["id"] == int(title)]
    if not match:
        conn.close()
        return (f"Группы «{title}» нет. Заведённые: "
                + (", ".join(t["title"] for t in rows) or "ни одной"))
    s = ledger.team_summary(conn, match[0]["id"])
    conn.close()
    t = s["team"]
    lines = [f"{t['title']} ({t['flow']})",
             f"Цель: {t['goal'] or '—'} вовлечений, сделано {s['count']}, "
             f"осталось {s['left']}",
             f"Принесено {_money(s['brought'])}, штрафы {_money(s['fines'])}",
             "", "Состав:"]
    for uid in s["ids"]:
        per = s["per"].get(uid, {})
        prof = corpus.profile_of(uid)
        state = f"{prof['status']}, молчит {prof['silent_days']} дн." if prof else "нет в чатах"
        lines.append(f"· {corpus.name_of(uid)} — {per.get('count', 0)} вовлечений, "
                     f"{_money(per.get('brought'))}; {state}")
    return _cut("\n".join(lines))


def flow_overview() -> str:
    """Общая сводка: сколько людей, сообщений, активных и выпавших,
    итоги по вовлечениям и деньгам, до какой даты собраны данные."""
    ov = corpus.overview()
    conn = store.connect()
    flow = ledger.flow_totals(conn)
    teams_n = len(ledger.teams(conn, with_archived=False))
    done = conn.execute("SELECT COUNT(*) n FROM analyses WHERE status='готов'").fetchone()["n"]
    conn.close()
    return _cut("\n".join([
        f"Данные собраны до {_date(str(ov['last']))}, начало — {_date(str(ov['first']))}.",
        f"Чатов {len(ov['chats'])}, сообщений {ov['messages']}, участников {ov['participants']}.",
        f"Голосом и кружками {ov['spoken']}, расшифровано {ov['transcribed']}, "
        f"это {ov['audio_hours']} ч звука.",
        f"Активны {ov['active']}, притихли {ov['quiet']}, выпали {ov['lost']}.",
        f"Вовлечений в журнале {flow['count']} у {flow['people']} человек, "
        f"принесено {_money(flow['brought'])}, штрафы {_money(flow['fines'])}.",
        f"Групп заведено {teams_n}. Готовых разборов и портретов {done}.",
    ]))


def search_messages(query: str, who: str = "", limit: int = 12) -> str:
    """Поиск по переписке сообщества: выдержки с автором и датой.
    Можно ограничить одним человеком. Полезно, когда нужны факты из чатов."""
    if not QUOTES:
        return "Выдержки из переписки в этом канале отключены."
    ids = None
    if who.strip():
        p, err = _one(who)
        if p is None:
            return err
        ids = [p["user_id"]]
    conn = store.connect()
    hits = corpus.search(conn, query, user_ids=ids, limit=max(1, min(limit, 30)))
    conn.close()
    if not hits:
        return f"По запросу «{query}» в переписке ничего нет."
    lines = [f"Найдено {len(hits)} (показаны по релевантности):"]
    for h in hits:
        text = re.sub(r"</?mark>", "", h["excerpt"]).replace("\n", " ")
        lines.append(f"· {h['when']}, {h['author']}, {h['chat']}: {text}")
    return _cut("\n".join(lines))


def involvement_mentions(who: str = "", limit: int = 20) -> str:
    """Объявления о вовлечениях в чатах («Сенсационные новости», «+1 вовлечение»).
    Это источник, по которому можно сверить журнал: вовлечения объявляли
    постами, а в журнал их пока не переносили."""
    variants: list[str] = []
    label = ""
    if who.strip():
        p, err = _one(who)
        if p is None:
            return err
        label = p["name"]
        # Ищем и так, как человека назвали в вопросе, и так, как он записан
        # в выгрузке: половина людей там латиницей, а спрашивают по-русски.
        for source in (who, p["name"], p.get("username") or ""):
            variants += [w.lower() for w in re.findall(r"[^\W\d_]{3,}", source)]
        variants = list(dict.fromkeys(variants))
        if not variants:
            return f"По «{who}» нет имени, по которому можно искать упоминания."

    conn = store.connect()
    rows: list[Any] = []
    for variant in (variants or [""]):
        expr = f'"{variant}"* AND "вовлеч"*' if variant else '"вовлеч"*'
        rows += conn.execute(
            "SELECT date, user_id, text FROM corpus_fts WHERE corpus_fts MATCH ? "
            "ORDER BY date DESC LIMIT 200", (expr,)).fetchall()
    conn.close()

    seen, uniq = set(), []
    for r in rows:
        low = r["text"].lower()
        # FTS ищет по началу слова, а в чатах имя часто внутри логина
        # (@ivanpetrov). Поэтому вторым проходом проверяем вхождение.
        if variants and not any(v in low for v in variants):
            continue
        key = (r["date"], r["text"][:40])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    uniq.sort(key=lambda r: r["date"], reverse=True)

    if not uniq:
        return ("Объявлений о вовлечениях по этому человеку в чатах нет."
                if who else "Упоминаний вовлечений в чатах нет.")
    head = (f"Упоминаний вовлечений в чатах: {len(uniq)}"
            + (f" (искали «{label}»)" if label else ""))
    lines = [head, "Это объявления из переписки, а не записи журнала."]
    for r in uniq[:max(1, min(limit, 40))]:
        text = " ".join(r["text"].split())[:180]
        lines.append(f"· {_date(r['date'])}, {corpus.name_of(r['user_id'])}: {text}")
    return _cut("\n".join(lines))


def read_analysis(who: str, kind: str = "ментору") -> str:
    """Готовый разбор человека. kind=«ментору» — как с ним работать и чем
    мотивировать; kind=«участнику» — портрет, написанный для него самого."""
    p, err = _one(who)
    if p is None:
        return err
    kind = "участнику" if "участ" in kind else "ментору"
    conn = store.connect()
    row = analysis.latest(conn, p["user_id"], kind)
    conn.close()
    if row is None or row["status"] != "готов":
        ready = analysis.readiness(p["user_id"])
        return (f"Разбора ({kind}) у {p['name']} нет. Материала {ready['level']}: "
                f"{ready['reason']}. Разбор запускается кнопкой в карточке "
                f"участника в приложении.")
    return _cut(f"Разбор {p['name']} ({kind}, от {_date(row['finished_at'])}):\n\n"
                + row["text"], 6000)


TOOLS = [find_person, person_card, quiet_list, involvements_top, teams_overview,
         team_card, flow_overview, search_messages, involvement_mentions,
         read_analysis]

BY_NAME = {f.__name__: f for f in TOOLS}
