#!/usr/bin/env python3
"""Перенос вовлечений из объявлений в чатах в журнал приложения.

Вовлечения объявляли постами: «Сенсационные новости — Света +1 вовлечение
@sveta_member». Журнал при этом заполняется руками и пуст, поэтому в
карточках стоят нули. Скрипт читает объявления, приписывает их людям
и предлагает записи для журнала.

    ./involve_import.py                 # разбор и отчёт, в базу ничего не пишет
    ./involve_import.py --plans         # заодно показать посты-планы, которые пропущены
    ./involve_import.py --unmatched     # показать, кого не удалось опознать
    ./involve_import.py --apply         # записать в журнал (повторный запуск не дублирует)

Что считается фактом, а что нет, решают шаблоны ниже. Планы
(«обязательства», «ждём вовлечение», «дедлайн») и ежедневные сводки
с общим счётом в журнал не идут: сводки нужны как контроль — их итог
сравнивается с суммой разобранных объявлений.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Any

from pathlib import Path

from app import assistant, corpus, ledger, store

# Ручные привязки логинов к людям: то, что из выгрузки не выводится.
MANUAL = Path(__file__).resolve().parent / "logins.txt"

# ------------------------------------------------------------- распознавание

# Явная прибавка: «+1 вовлечение», «+ 2 вовлеченных», «➕1 вовлечённый».
PLUS = re.compile(r"[+➕]\s*(\d{1,2})\s*вовлеч", re.I)

# Пост-факт: так объявляют уже случившееся.
FACT = re.compile(r"сенсацион|поздравля|у нас\s*[+➕]|давайте отметим|"
                  r"вспомним наших героев|поделиться крутой новостью|"
                  r"важные новости|героев дня", re.I)

# Пост-план: обещания и ожидания. В журнал не идут — это не результат.
PLAN = re.compile(r"обязательств|дедлайн|цена слово|ждем вовлеч|ждём вовлеч|"
                  r"ждем от|ждём от|нужно вовлечь|планку|планка от|"
                  r"до конца \d|включительно", re.I)

# Ежедневная сводка с общим счётом: контроль, а не источник записей.
DIGEST = re.compile(r"цели потока|итог:|вовлекли\s*-?\s*\d+\s*человек", re.I)

# Пересказ вчерашнего дня: дата события — предыдущая.
RECAP = re.compile(r"вчерашний день|еще раз вспомним|ещё раз вспомним", re.I)

# «У каждого из вас по +1» — прибавка относится к каждому упомянутому.
EACH = re.compile(r"у каждого|каждому по|по\s*[+➕]\s*\d", re.I)

# «(уже 24-е)» — накопленный номер вовлечения. Он же различает два
# объявления об одном человеке в один день.
SEQ = re.compile(r"уже\s*(\d{1,3})\s*-?[её]|теперь\s*(\d{1,3})|\((\d{1,3})-[её]", re.I)

USERNAME = re.compile(r"@([A-Za-z][A-Za-z0-9_]{3,31})")
# Имя — не короче четырёх букв: иначе «Так держать!» становится фамилией.
CYRILLIC = re.compile(r"\b([А-ЯЁ][а-яё]{3,})\b")

# Слова с большой буквы, которые именами не являются.
NOT_NAME = {"Сенсационные", "Новости", "Вовлеченным", "Вовлечение", "Спасибо",
            "Поздравляем", "Поздравляю", "Ценно", "Гордимся", "Продолжаем",
            "Вершины", "Вершина", "Вершину", "Казани", "Краснодаре", "Это",
            "Ты", "Вы", "Именно", "Такие", "Твой", "Твоя", "Мощно", "Супер",
            "Командная", "Первое", "Очень", "Каждый", "Впереди", "Пусть",
            "Продолжай", "Забирай", "Мужчина", "Девочки", "Ребята", "Ждем",
            "Ждём", "Давайте", "Важные", "Вчерашний", "Есть", "Обязательства",
            "Дедлайн", "Цена", "Планка", "Итог", "Лидеры", "Лидер",
            "Если", "Какой", "Кажется", "Итого", "Ребят", "Ребята", "Сегодня",
            "Больше", "Хорошие", "Спонсор", "Твоё", "Твои", "Сильный",
            "Здесь", "Теперь", "Ждать", "Вместе", "Сразу", "Просто", "Когда",
            "Внимание", "Результат", "Респект", "Молодец", "Браво"}


def _segments(text: str) -> list[str]:
    """Пост → куски, в каждом из которых человек и его прибавка рядом.
    Списки героев дня пишут построчно, поэтому строка — естественная граница."""
    parts: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            parts.append(line)
    return parts or [text]


def _names_in(chunk: str) -> tuple[list[str], list[str]]:
    users = [u.lower() for u in USERNAME.findall(chunk)]
    names = [n for n in CYRILLIC.findall(chunk) if n not in NOT_NAME]
    return users, names


# ---------------------------------------------------------------- опознание

class Directory:
    """Кто есть кто: @логин → участник.

    В выгрузке Telegram Desktop логинов нет вовсе — ни у кого. Зато сами
    участники и организаторы пишут их в переписке рядом с именами:
    «Евгений Тарасов @evg_member», «⭐️РУСТАМ @Rustam_777». По таким парам
    словарь и собирается — по всему корпусу, а не только по объявлениям:
    опознание человека не зависит от того, факт перед нами или план.
    """

    def __init__(self) -> None:
        self.people = corpus.participants()
        # Логин прямо из телеграма — самое надёжное, что вообще бывает.
        # В выгрузке Desktop логинов нет, но живое подключение их отдаёт,
        # и тогда гадать по именам рядом уже не нужно.
        self.by_login = {p["username"].lower(): p for p in self.people
                         if p.get("username")}
        self.by_skeleton = {}
        for p in self.people:
            self.by_skeleton[assistant._skeleton(p["name"]).replace(" ", "")] = p
        self.map: dict[str, dict[str, Any]] = {}
        self.votes: dict[str, Counter] = defaultdict(Counter)
        self.conflicts: dict[str, set[str]] = defaultdict(set)
        self.disagreements: dict[str, tuple[str, str]] = {}
        self.manual: set[str] = set()
        self.bad_manual: list[str] = []
        self.in_chat: dict[int, set[int]] = defaultdict(set)
        for uid, rows in corpus._data()["by_user"].items():
            for r in rows:
                self.in_chat[r["chat_id"]].add(uid)

    NEAR = 25          # столько символов между именем и логином считаем «рядом»
    ENOUGH_VOTES = 3   # двух случайных встреч имени и логина тоже мало

    def learn_corpus(self) -> None:
        """Пройти по всей переписке и собрать пары «логин — имя».

        Голос тем весомее, чем ближе имя к логину: «Евгений Тарасов @evg_member»
        — это связка, а имя в другом конце абзаца — совпадение. Одиночное
        далёкое совпадение связкой не становится.
        """
        for rows in corpus._data()["rows_by_chat"].values():
            for r in rows:
                text = r["content"]
                if not text or "@" not in text:
                    continue
                users = list(USERNAME.finditer(text))
                if not users:
                    continue
                names = list(CYRILLIC.finditer(text))
                for um in users:
                    u = um.group(1).lower()
                    near = None
                    for nm in names:
                        if nm.group(1) in NOT_NAME:
                            continue
                        gap = (nm.start() - um.end() if nm.start() > um.end()
                               else um.start() - nm.end())
                        if gap > self.NEAR:
                            continue
                        if near is None or gap < near[0]:
                            near = (gap, nm.group(1))
                    # В списках людей рядом стоит много имён, и правильное —
                    # только ближайшее. Поэтому берём именно его, а не первое.
                    if near is None:
                        continue
                    found = self.strict(near[1], r["chat_id"])
                    if found:
                        self.votes[u][found["user_id"]] += 1

        for username, votes in self.votes.items():
            (uid, top), = votes.most_common(1)
            if top < self.ENOUGH_VOTES:
                continue
            rival = [u for u, n in votes.items() if u != uid and n >= top]
            if rival:
                self.conflicts[username] = {corpus.name_of(u) for u in [uid, *rival]}
                continue
            person = corpus.profile_of(uid)
            if person:
                self.map[username] = person

        # Логин, собранный из имени и фамилии, — свидетельство сильнее
        # голосов: @ivanpetrov это Ivan Petrov, сколько бы раз рядом
        # ни оказывалось чужое имя. Несогласия показываем в отчёте.
        for username in list(self.votes):
            letters, strength = self.by_letters(username)
            if not letters or strength < 2:
                continue
            current = self.map.get(username)
            if current and current["user_id"] != letters["user_id"]:
                self.disagreements[username] = (
                    f"{current['name']} (id {current['user_id']})",
                    f"{letters['name']} (id {letters['user_id']})")
            self.map[username] = letters

        # Настоящие логины перекрывают всё, что собрано голосованием.
        self.map.update(self.by_login)
        self.load_manual()

    def load_manual(self) -> None:
        """Ручные привязки логинов. Их не вывести из выгрузки — организатор
        знает их наизусть, и одна строка в файле закрывает вопрос навсегда.

        Формат файла logins.txt: «@alex_member = 123456789» или «@alex_member = Имя».
        """
        path = MANUAL
        if not path.exists():
            return
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#")[0].strip()
            if not line or "=" not in line:
                continue
            left, right = (x.strip() for x in line.split("=", 1))
            username = left.lstrip("@").lower()
            found = assistant.resolve(right)
            if len(found) == 1:
                self.map[username] = found[0]
                self.manual.add(username)
            else:
                self.bad_manual.append(line)

    def strict(self, name: str, chat_id: int | None) -> dict[str, Any] | None:
        """Имя из текста → участник, но только наверняка: без совпадений
        по началу слова и только среди писавших в этом чате."""
        found = assistant.resolve(name, loose=False)
        if chat_id:
            found = [p for p in found if p["user_id"] in self.in_chat[chat_id]]
        return found[0] if len(found) == 1 else None

    def by_letters(self, username: str) -> tuple[dict[str, Any] | None, int]:
        """@ivanpetrov → Ivan Petrov: логин собран из имени и фамилии.

        Возвращает человека и силу совпадения — сколько слов его имени
        нашлось в логине. Два слова («dmitry» и «magala») — это почти
        доказательство; одно («vladimir») — лишь намёк.
        """
        u = assistant._skeleton(username).replace(" ", "")
        best, best_strength, best_len = None, 0, 0
        for p in self.people:
            words = [w for w in assistant._skeleton(p["name"]).split() if len(w) >= 4]
            if not words:
                continue
            hit = [w for w in words if w in u]
            strength = len(hit)
            if not strength:
                continue
            length = sum(len(w) for w in hit)
            if (strength, length) > (best_strength, best_len):
                best, best_strength, best_len = p, strength, length
        return best, best_strength

    def person(self, username: str = "", names: list[str] | None = None,
               chat_id: int | None = None):
        if username:
            known = self.by_login.get(username.lower())
            if known:
                return known
            if username in self.map:
                return self.map[username]
            guess, _ = self.by_letters(username)
            if guess:
                self.map[username] = guess
                return guess
        for name in (names or []):
            # «Евгений» подходит нескольким — но в этом чате писал один.
            person = self.strict(name, chat_id)
            if person:
                return person
        return None


# ------------------------------------------------------------------- разбор

# Строка табло в ежедневной сводке: «Олег @oleg_member – 10», «Игорь @igor_member – 0».
ROW = re.compile(r"@([A-Za-z][A-Za-z0-9_]{3,31})\s*[–—-]+\s*(\d{1,3})")
BOARD_MIN = 3      # столько строк табло отличают сводку со счётом от обычной


# В сводках числа пишут эмодзи-цифрами: «5️⃣3️⃣человек». Служебные символы
# нужно убрать, иначе от числа 53 остаётся 5.
EMOJI_DIGIT = {0xFE0F: None, 0x20E3: None}

# Общий счёт потока: первая строка «Вовлекли N человек» в сводке.
FLOW = re.compile(r"вовлекли[^\d\n]{0,14}(\d{1,3})\s*челов", re.I)
EXTRA = re.compile(r"подарок[^\d\n]{0,30}(\d{1,3})\s*челов"
                   r"|выбывшие[\s\S]{0,40}?вовлекли[^\d\n]{0,14}(\d{1,3})",
                   re.I)


def _norm(text: str) -> str:
    return text.translate(EMOJI_DIGIT)


def flow_count(row: dict[str, Any]) -> int | None:
    m = FLOW.search(_norm(row["content"]))
    return int(m.group(1)) if m else None


def outside_board(row: dict[str, Any]) -> int:
    """Вовлечения, которых в табло нет: подарок менторов и выбывшие."""
    out = 0
    for m in EXTRA.finditer(_norm(row["content"])):
        out += int(m.group(1) or m.group(2) or 0)
    return out


def split_posts() -> dict[str, list[dict[str, Any]]]:
    """Все сообщения про вовлечения разложить по назначению."""
    d = corpus._data()
    rows = [r for rows in d["rows_by_chat"].values() for r in rows
            if r["content"] and "вовлеч" in r["content"].lower()]
    rows.sort(key=lambda r: r["date"])

    out: dict[str, list[dict[str, Any]]] = {"facts": [], "plans": [],
                                            "digests": [], "other": []}
    for r in rows:
        text = r["content"]
        if DIGEST.search(text):
            out["digests"].append(r)
        elif PLAN.search(text):
            out["plans"].append(r)
        elif FACT.search(text) and PLUS.search(text):
            out["facts"].append(r)
        else:
            out["other"].append(r)
    return out


def boards(digests: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    """Сводки с табло по людям: чат → список замеров по дням.

    В ММ6 такое табло публикуют каждый день — это официальный счёт потока,
    а не пересказ. Разница между соседними днями и есть новые вовлечения.
    """
    per_chat: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
    for r in digests:
        pairs = ROW.findall(r["content"])
        if len(pairs) < BOARD_MIN:
            continue
        day = r["date"][:10]
        counts = {u.lower(): int(n) for u, n in pairs}
        old = per_chat[r["chat_id"]].get(day)
        # За день бывает несколько сводок: берём последнюю по номеру сообщения.
        if old is None or r["msg_id"] > old["msg_id"]:
            per_chat[r["chat_id"]][day] = {"day": day, "msg_id": r["msg_id"],
                                           "counts": counts, "row": r}
    return {chat: [per_chat[chat][d] for d in sorted(per_chat[chat])]
            for chat in per_chat}


def board_events(all_boards: dict[int, list[dict[str, Any]]],
                 directory: Directory) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Табло → вовлечения с датами. Счёт вырос на k — значит k вовлечений."""
    events: list[dict[str, Any]] = []
    unknown: dict[str, dict[str, Any]] = {}
    for chat_id, days in all_boards.items():
        seen: dict[str, int] = {}
        for i, snap in enumerate(days):
            for login, count in snap["counts"].items():
                was = seen.get(login)
                seen[login] = count
                # Первый замер — весь накопленный счёт: когда именно эти
                # вовлечения случились, табло не говорит.
                grew = count if was is None else count - was
                if grew <= 0:
                    continue
                person = directory.map.get(login)
                if person is None:
                    slot = unknown.setdefault(login, {"count": 0, "chat": chat_id})
                    slot["count"] = max(slot["count"], count)
                    continue
                events.append({
                    "uid": person["user_id"], "name": person["name"],
                    "day": snap["day"], "count": grew,
                    "chat_id": chat_id, "msg_id": snap["msg_id"],
                    "username": login, "seq": count, "source": "сводка",
                    "author": corpus.name_of(snap["row"]["user_id"]),
                    "text": (f"по ежедневной сводке за {_ru(snap['day'])}: "
                             f"у @{login} стало {count}"
                             + ("" if was is None else f" (было {was})")),
                    "first": was is None,
                })
    return events, unknown


def announcement_events(facts: list[dict[str, Any]], directory: Directory
                        ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Объявления «+1 вовлечение» → вовлечения с датами."""
    events: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    for r in facts:
        text = r["content"]
        day = r["date"][:10]
        if RECAP.search(text):
            day = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
        post_users, post_names = _names_in(text)
        seq_m = SEQ.search(text)
        seq = next((int(g) for g in seq_m.groups() if g), None) if seq_m else None
        found_here = False

        for chunk in _segments(text):
            m = PLUS.search(chunk)
            if not m:
                continue
            count = int(m.group(1))
            if count < 1:
                continue
            users, names = _names_in(chunk)
            # Имя и логин часто стоят на разных строках одного объявления:
            # «Дмитрий +1 вовлечение» строкой выше, «@ivanpetrov» ниже.
            if not users and len(post_users) == 1:
                users = list(post_users)
            if not names:
                names = post_names
            if not users and not names:
                continue
            if EACH.search(chunk) and users:
                targets = [directory.person(u, names, r["chat_id"]) for u in users]
            elif users:
                targets = [directory.person(users[0], names, r["chat_id"])]
            else:
                targets = [directory.person("", names, r["chat_id"])]
            for person, username in zip(targets, users or [""]):
                if person is None:
                    unmatched.append({"row": r, "chunk": chunk, "count": count,
                                      "users": users, "names": names})
                    continue
                found_here = True
                events.append({
                    "uid": person["user_id"], "name": person["name"],
                    "day": day, "count": count, "chat_id": r["chat_id"],
                    "msg_id": r["msg_id"], "username": username,
                    "seq": seq,
                    "source": "объявление",
                    "author": corpus.name_of(r["user_id"]),
                    "text": (f"объявление в чате от {_ru(day)}: "
                             + " ".join(chunk.split())[:120]),
                })
        if not found_here and not any(u["row"] is r for u in unmatched):
            unmatched.append({"row": r, "chunk": " ".join(text.split())[:160],
                              "count": 0, "users": [], "names": []})
    return events, unmatched


def dedupe(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Одно вовлечение объявляли по несколько раз: отдельным постом, потом
    в списке героев дня. Поэтому на человека за день берём не сумму,
    а максимум — так занижение вероятнее завышения, и это честнее.

    Если в объявлении назван накопленный номер, два вовлечения одного дня
    различимы: «(уже 24-е)» и «(уже 25-е)» — это два разных.
    """
    best: dict[tuple[int, str, int | None], dict[str, Any]] = {}
    for e in events:
        key = (e["uid"], e["day"], e.get("seq"))
        cur = best.get(key)
        if cur is None or e["count"] > cur["count"]:
            best[key] = e
    return sorted(best.values(), key=lambda e: (e["day"], e["name"]))


def fill_gaps(records: list[dict[str, Any]], from_posts: list[dict[str, Any]],
              board_chats: set[int]) -> list[dict[str, Any]]:
    """Дотянуть счёт до номера, названного в чатах.

    В объявлениях пишут накопленный номер: «уже 29-е». Значит вовлечений
    было 29, даже если в выгрузке сохранилось 18 объявлений. Разницу
    добавляем записями с датой того объявления и честной пометкой: это
    восстановленный счёт, а не отдельное найденное событие.
    """
    have = Counter()
    for e in records:
        have[e["uid"]] += e["count"]
    top: dict[int, dict[str, Any]] = {}
    for e in from_posts:
        if e["chat_id"] in board_chats or not e.get("seq"):
            continue
        cur = top.get(e["uid"])
        if cur is None or e["seq"] > cur["seq"]:
            top[e["uid"]] = e
    extra = []
    for uid, e in top.items():
        gap = e["seq"] - have[uid]
        if gap <= 0:
            continue
        extra.append({**e, "count": gap, "source": "счёт из чатов",
                      "msg_id": f"{e['msg_id']}-счёт",
                      "text": (f"восстановлено по счёту из чатов: в объявлении "
                               f"от {_ru(e['day'])} назван номер {e['seq']}, "
                               f"а отдельных объявлений сохранилось "
                               f"{have[uid]}")})
    return extra


def collect(fill: bool = False) -> dict[str, Any]:
    posts = split_posts()
    directory = Directory()
    directory.learn_corpus()

    all_boards = boards(posts["digests"])
    from_board, unknown = board_events(all_boards, directory)
    from_posts, unmatched = announcement_events(posts["facts"], directory)

    # Где есть ежедневное табло, оно и есть истина: объявления в этом чате
    # только дублируют его, а часть вовлечений в них вообще не попадала.
    board_chats = {c for c, days in all_boards.items() if len(days) >= 2}
    records = from_board + dedupe([e for e in from_posts
                                   if e["chat_id"] not in board_chats])
    filled = fill_gaps(records, from_posts, board_chats) if fill else []
    records = records + filled
    records.sort(key=lambda e: (e["day"], e["name"]))

    return {"posts": posts, "directory": directory, "boards": all_boards,
            "board_chats": board_chats, "from_board": from_board,
            "from_posts": from_posts, "records": records,
            "unknown": unknown, "unmatched": unmatched, "filled": filled}


def _ru(day: str) -> str:
    y, m, d = day.split("-")
    return f"{d}.{m}.{y}"


# ------------------------------------------------------------------- отчёт

def report(data: dict[str, Any], show_plans: bool, show_unmatched: bool,
           detail: str) -> None:
    posts = data["posts"]
    records = data["records"]
    titles = {c["id"]: c["title"] for c in corpus._data()["chats"]}

    print(f"Сообщений со словом «вовлеч»: {sum(len(v) for v in posts.values())}")
    print(f"  ежедневных сводок: {len(posts['digests'])}, "
          f"из них с табло по людям: {sum(len(d) for d in data['boards'].values())}")
    print(f"  объявлений-фактов: {len(posts['facts'])}")
    print(f"  планов и обязательств (пропущены): {len(posts['plans'])}")
    print(f"  прочих упоминаний: {len(posts['other'])}")

    print("\nИсточники по чатам:")
    for chat_id, title in titles.items():
        src = ("ежедневное табло в сводках" if chat_id in data["board_chats"]
               else "объявления «+N вовлечение»")
        n = sum(e["count"] for e in records if e["chat_id"] == chat_id)
        print(f"  {title}: {src} — {n} вовлечений")

    per = Counter()
    for e in records:
        per[e["name"]] += e["count"]
    print(f"\nВсего к переносу: {sum(per.values())} вовлечений "
          f"у {len(per)} человек")
    for name, n in per.most_common():
        print(f"  {n:3d}  {name}")

    if data["filled"]:
        print(f"\nВосстановлено по счёту из чатов: "
              f"{sum(e['count'] for e in data['filled'])} записей у "
              f"{len(data['filled'])} человек — это разница между номером "
              f"в объявлении («уже 29-е») и числом сохранившихся объявлений.")
        for e in sorted(data["filled"], key=lambda e: -e["count"]):
            print(f"  {e['name']}: +{e['count']} к {_ru(e['day'])} "
                  f"(номер {e['seq']})")

    first = [e for e in data["from_board"] if e.get("first")]
    if first:
        print(f"\nИз них {sum(e['count'] for e in first)} записей встали на дату "
              f"первой сводки с табло: до неё счёт уже был, а когда именно "
              f"случились те вовлечения, сводка не говорит.")

    print("\nСверка с общим счётом потока:")
    for chat_id, days in data["boards"].items():
        last = days[-1]
        board_sum = sum(last["counts"].values())
        flow = flow_count(last["row"])
        extra = outside_board(last["row"])
        line = (f"  {titles.get(chat_id, chat_id)}, сводка за {_ru(last['day'])}: "
                f"табло по людям даёт {board_sum}")
        if flow is not None:
            rest = flow - board_sum - extra
            line += (f", общий счёт потока {flow}, вне табло (подарок менторов, "
                     f"выбывшие) {extra}")
            line += (" — сходится" if rest == 0 else f", необъяснённых {rest}")
        print(line)
        mine = sum(e["count"] for e in data["records"] if e["chat_id"] == chat_id)
        print(f"     перенесено {mine}: разница — вовлечения неопознанных логинов")
    for chat_id in titles:
        if chat_id in data["boards"]:
            continue
        last = max((r for r in data["posts"]["digests"] if r["chat_id"] == chat_id
                    and flow_count(r) is not None),
                   key=lambda r: r["date"], default=None)
        mine = sum(e["count"] for e in data["records"] if e["chat_id"] == chat_id)
        if last is None:
            print(f"  {titles[chat_id]}: сводок с общим счётом нет, "
                  f"перенесено {mine} по объявлениям")
            continue
        flow = flow_count(last)
        print(f"  {titles[chat_id]}, сводка за {_ru(last['date'][:10])}: общий счёт "
              f"потока {flow}, перенесено по объявлениям {mine}")
        if flow is not None and mine > flow:
            print("     ВНИМАНИЕ: перенесено больше официального счёта потока. "
                  "Личные номера в объявлениях («уже 29-е») считают и вовлечения "
                  "на ступенях, которые в счёт потока не идут, — участники это "
                  "обсуждали в чате. Если нужен счёт потока, не используйте --fill.")
        else:
            print("     табло по людям в этом чате не публиковали, поэтому перенос — "
                  "это нижняя граница: сохранились не все объявления.")

    seqs: dict[int, int] = {}
    for e in data["from_posts"]:
        if e.get("seq"):
            seqs[e["uid"]] = max(seqs.get(e["uid"], 0), e["seq"])
    if seqs:
        mine = Counter()
        for e in data["records"]:
            mine[e["uid"]] += e["count"]
        print("\nНомера из объявлений («уже 25-е» — значит было не меньше 25):")
        for uid, n in sorted(seqs.items(), key=lambda kv: -kv[1]):
            print(f"  {corpus.name_of(uid)}: не меньше {n}, "
                  f"перенесено {mine.get(uid, 0)}")

    if data["unknown"]:
        print("\nЛогины из табло, которых нет в словаре "
              "(их вовлечения не перенесены):")
        for login, slot in sorted(data["unknown"].items(),
                                  key=lambda kv: -kv[1]["count"]):
            if slot["count"]:
                print(f"  @{login}: по табло {slot['count']} вовлечений")
        zero = [u for u, s in data["unknown"].items() if not s["count"]]
        if zero:
            print(f"  с нулём (перенос не нужен): {', '.join('@' + u for u in sorted(zero))}")
        print(f"Кто это — знает организатор. Строка в {MANUAL.name} вида "
              f"«@login = Имя» закрывает вопрос навсегда.")

    if show_unmatched:
        print(f"\nНеопознанные объявления ({len(data['unmatched'])}):")
        for u in data["unmatched"]:
            r = u["row"]
            print(f"  {_ru(r['date'][:10])} {corpus.name_of(r['user_id'])}: "
                  f"{u['chunk'][:120]}")
    if show_plans:
        print(f"\nПропущенные планы ({len(posts['plans'])}):")
        for r in posts["plans"]:
            print(f"  {_ru(r['date'][:10])} {corpus.name_of(r['user_id'])}: "
                  f"{' '.join(r['content'].split())[:120]}")
    if detail:
        found = assistant.resolve(detail)
        for p in found[:3]:
            print(f"\nЗаписи по {p['name']}:")
            for e in records:
                if e["uid"] == p["user_id"]:
                    print(f"  {_ru(e['day'])} +{e['count']} ({e['source']}) — "
                          f"{e['text'][:120]}")

    d = data["directory"]
    print(f"\nСловарь логинов: опознано {len(d.map)}"
          + (f", из них прямо из телеграма {len(d.by_login)}" if d.by_login else "")
          + (f", вручную {len(d.manual)}" if d.manual else ""))
    for line in d.bad_manual:
        print(f"  строка из {MANUAL.name} не разобрана: {line}")
    if d.disagreements:
        print("Спорные логины (взято написание логина):")
        for u, (by_votes, by_letters) in d.disagreements.items():
            print(f"  @{u}: рядом писали «{by_votes}», по буквам — «{by_letters}»")


# -------------------------------------------------------------------- запись

def apply(records: list[dict[str, Any]], author: str) -> tuple[int, int]:
    conn = store.connect()
    added = skipped = 0
    conn.execute("BEGIN IMMEDIATE")
    for e in records:
        source = f"{e['chat_id']}/{e['msg_id']}"
        have = conn.execute(
            "SELECT COUNT(*) n FROM involvements WHERE tg_user_id=? AND source=?",
            (e["uid"], source)).fetchone()["n"]
        need = e["count"] - have
        if need <= 0:
            skipped += e["count"]
            continue
        for _ in range(need):
            conn.execute(
                "INSERT INTO involvements (tg_user_id, happened, amount, who, "
                "status, note, origin, added_by, source) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (e["uid"], e["day"], 0, None, "ожидание", e["text"][:300],
                 e["source"], author, source))
            added += 1
    conn.execute("COMMIT")
    conn.close()
    return added, skipped


def main() -> None:
    ap = argparse.ArgumentParser(description="перенос вовлечений из чатов")
    ap.add_argument("--apply", action="store_true", help="записать в журнал")
    ap.add_argument("--plans", action="store_true", help="показать пропущенные планы")
    ap.add_argument("--unmatched", action="store_true", help="показать неопознанные объявления")
    ap.add_argument("--detail", default="", help="показать все записи одного человека")
    ap.add_argument("--fill", action="store_true",
                    help="дотянуть счёт до номера, названного в объявлениях")
    ap.add_argument("--author", default="импорт из чатов",
                    help="кто значится автором записей")
    args = ap.parse_args()

    store.init()
    data = collect(fill=args.fill)
    report(data, args.plans, args.unmatched, args.detail)

    if not args.apply:
        print("\nВ базу ничего не записано. Запись — с ключом --apply.")
        return
    added, skipped = apply(data["records"], args.author)
    print(f"\nЗаписано в журнал: {added}. Уже было (пропущено): {skipped}.")
    print("Статус записей — «ожидание», сумма 0 ₽: объявление и табло "
          "подтверждают факт вовлечения, но не оплату. Суммы вносятся отдельно.")


if __name__ == "__main__":
    main()
