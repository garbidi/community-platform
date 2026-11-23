"""«Сенсационные новости» сами: оплата в Тильде → пост о вовлечении + фото чата «вовлекли N».

Как договорились на созвоне 26.10.2026 (Лилия, Глеб) и в переписке 02.11:
- заказы приходят аккаунту сообщества (Марья) от бота Тильды (@TildaFormsBot);
- «Заказ оплачен» (карта: Точка, Альфа) — публикуем сразу;
- «Безналичная оплата» — это только запрос счёта: ждём, пока Лилия или Полина
  напишут «оплата пришла» в группе «Безналичные оплаты» (переслать заказ или
  назвать человека);
- кто вовлёк — поле Input_3 формы, свободным текстом; ищем среди участников
  идущих потоков. Не уверены — пост ждёт, организатор выбирает на странице;
- пост: фото вовлёкшего + текст от Claude по примерам Марьи; затем фото чата
  меняется на «вовлекли N» (sens_avatar), N — счётчик на платформе.

Всё, что уходит в телеграм, проходит через замок отправки (tg_account.sending_allowed).
Пока идёт проверка, чат для новостей — тестовая группа «Сенсационные новости бот».
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from . import flows, store, tg_account

MSK = timezone(timedelta(hours=3))

TILDA_CHAT = 100000001           # @TildaFormsBot в личке аккаунта сообщества
BANK_CHAT = -5000000001          # «Безналичные оплаты»: Лилия, Полина, Марья, Станислав
TEST_CHAT = -5000000002          # «Сенсационные новости бот» — проверка

DEFAULTS = {
    "sens:on": "0",               # публиковать самим (иначе только читаем и показываем)
    "sens:chat": str(TEST_CHAT),  # куда пост
    "sens:photo_chat": str(TEST_CHAT),  # чьё фото менять
    "sens:avatar": "1",           # менять фото чата
    "sens:count": "86",           # сколько вовлекли участники (на фото) — до этой оплаты
    # вовлечения менторов (созвон 03.11, Лилия): новость — в чат менторов, фото не меняется,
    # в копилку потока не идут, но счёт ведём отдельно
    "sens:mentor_chat": str(TEST_CHAT),
    "sens:mentor_count": "0",
    "sens:goal": "94",            # планка (бледная на фото)
    "sens:flow": "",              # поток для примеров и записи в «Посты»; пусто — текущий
    "sens:bank_chat": str(BANK_CHAT),  # чат безнала (меняется сам, если группа стала супергруппой)
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS sens_orders (
    id INTEGER PRIMARY KEY,
    msg_id INTEGER NOT NULL UNIQUE,        -- сообщение бота Тильды
    at TEXT, order_no TEXT,
    kind TEXT,                             -- карта | безнал | заявка
    amount INTEGER, product TEXT,
    name TEXT, phone TEXT, phone_d TEXT, tg TEXT, city TEXT, engaged TEXT,
    person_id INTEGER, sure INTEGER NOT NULL DEFAULT 0, candidates TEXT,
    status TEXT NOT NULL,                  -- архив | ждёт оплаты | кто вовлёк? | в очереди | публикуется | опубликовано | ошибка | пропущено | повтор
    paid_at TEXT, paid_note TEXT,
    post_id INTEGER, count_after INTEGER,
    note TEXT, decided_by TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS sens_bank (
    msg_id INTEGER PRIMARY KEY,            -- сообщение в «Безналичных оплатах»
    at TEXT, sender_id INTEGER, sender TEXT, text TEXT, reply_to INTEGER,
    marker INTEGER NOT NULL DEFAULT 0,     -- «оплата пришла»
    order_id INTEGER, note TEXT
);
"""

_RUN = threading.Lock()          # одна публикация за раз (счётчик и фото чата)


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def setting(conn, key: str) -> str:
    return store.get_setting(conn, key, DEFAULTS.get(key, "")) or DEFAULTS.get(key, "")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ----------------------------------------------------------------- имена

def _words(name: str | None) -> list[str]:
    return [w for w in re.split(r"[^a-zа-я]+", (name or "").lower().replace("ё", "е")) if w]


def _same(a: str, b: str) -> bool:
    """Одно слово имени: совпало целиком или по первым 4 буквам (Марков / Маркарян, Алексей / Глеб)."""
    return a == b or (len(a) >= 4 and len(b) >= 4 and a[:4] == b[:4])


def _match(cands: list, name: str | None) -> tuple[int | None, bool, list[int]]:
    """(кому, уверенно, похожие). Все слова имени совпали — уверенно.
    Похожие: каждое слово нашлось у человека (хотя бы одно — целиком);
    если слово одно («Сирин», «Гаджи») — оно должно совпасть целиком."""
    words = _words(name)
    if not words:
        return None, False, []
    exact = [p["id"] for p in cands if set(_words(p["name"])) == set(words)]
    if len(exact) == 1:
        return exact[0], True, exact
    near = []
    for p in cands:
        theirs = _words(p["name"])
        if all(any(_same(w, t) for t in theirs) for w in words) and any(w in theirs for w in words):
            near.append(p["id"])
    ids = exact or near
    return (ids[0] if len(ids) == 1 else None), False, ids[:5]


# ----------------------------------------------------------------- разбор заказа

def _field(text: str, name: str) -> str | None:
    m = re.search(rf"^{re.escape(name)}:\s*(.+)$", text, re.M)
    return m.group(1).strip() if m else None


def _digits(phone: str | None) -> str:
    d = re.sub(r"\D", "", phone or "")
    return d[-10:] if len(d) >= 10 else d


def parse(text: str) -> dict[str, Any] | None:
    """Сообщение бота Тильды → заказ. Не заказ — None."""
    text = text or ""
    if text.startswith("Содержание заявки"):
        return {"kind": "заявка", "name": _field(text, "name") or _field(text, "Name"),
                "phone": _field(text, "Phone"), "tg": _field(text, "telegram")}
    m = re.search(r"Заказ №(\d+)", text)
    if not m:
        return None
    prod = re.search(r"^1\.\s*(.+?):\s*(\d+)", text, re.M)
    amount = re.search(r"Сумма платежа:\s*(\d+)", text)
    paid = "Заказ оплачен" in text
    return {
        "order_no": m.group(1),
        "kind": "карта" if paid else ("безнал" if "Безналичная" in text else "другое"),
        "product": prod.group(1).strip() if prod else None,
        "amount": int(amount.group(1)) if amount else None,
        "name": _field(text, "Name"), "phone": _field(text, "Phone"),
        "city": _field(text, "Input"), "tg": _field(text, "Input_2"),
        "engaged": _field(text, "Input_3"),
    }


# ----------------------------------------------------------------- чтение чатов

def _read(chat_id: int, after: int, limit: int = 60) -> list[dict]:
    res = tg_account.read_messages(chat_id, None, limit=limit)
    if not res.get("ok"):
        raise RuntimeError(res.get("error") or "телеграм не ответил")
    return sorted((m for m in res["messages"] if m["id"] > after), key=lambda m: m["id"])


def candidates(conn) -> list[dict]:
    """Кто может вовлечь: участники идущих потоков и их менторы (у менторов id < 0 — минус id учётки)."""
    out = [dict(p) for p in conn.execute(
        "SELECT p.id, p.name, p.flow, p.source, p.status FROM people p JOIN flows f ON f.name=p.flow "
        "WHERE f.status='идёт' AND p.moved_to IS NULL ORDER BY p.flow, p.name")]
    for u in conn.execute(
            "SELECT DISTINCT u.id, u.name, u.tg_user_id FROM flow_staff fs JOIN users u ON u.id=fs.user_id "
            "JOIN flow_teams t ON t.id=fs.flow_team_id JOIN flows f ON f.id=t.flow_id "
            "WHERE f.status='идёт' AND u.active=1 ORDER BY u.name"):
        out.append({"id": -u["id"], "name": u["name"], "flow": "ментор", "source": "платформа",
                    "status": "ментор", "tg": u["tg_user_id"]})
    return out


def _person(conn, pid: int) -> dict | None:
    """Вовлёкший: имя, ник, телеграм, группа — для текста и фото."""
    from . import posts
    if pid > 0:
        p = conn.execute("SELECT p.*, t.title AS team_title FROM people p LEFT JOIN teams t ON t.id=p.team_id "
                         "WHERE p.id=?", (pid,)).fetchone()
        if not p:
            return None
        return {"name": posts._first_last(p["name"]), "nick": posts._nicks(conn, p["flow"]).get(p["id"]),
                "flow": p["flow"], "team": posts._team_short(p["team_title"]), "card": -p["id"], "tg": None}
    u = conn.execute("SELECT * FROM users WHERE id=?", (-pid,)).fetchone()
    if not u:
        return None
    nick = conn.execute("SELECT MAX(username) FROM chat_messages WHERE sender_id=? AND username IS NOT NULL",
                        (u["tg_user_id"] or 0,)).fetchone()[0]
    return {"name": u["name"], "nick": nick, "flow": "ментор потока", "team": "", "card": None, "tg": u["tg_user_id"]}


def _photo(conn, pid: int) -> str:
    from . import posts
    p = _person(conn, pid)
    if not p:
        raise RuntimeError("человека нет")
    if p["card"] is not None:
        return str(posts.avatar_file(conn, p["card"]))
    if not p["tg"]:
        raise RuntimeError("у ментора не указан телеграм")
    path = posts.MEDIA / f"avatar_{p['tg']}.jpg"
    posts.MEDIA.mkdir(parents=True, exist_ok=True)
    if not path.exists() and not tg_account.fetch_avatar_big(p["tg"], str(path)):
        raise RuntimeError("у ментора нет аватарки в телеграме")
    return str(path)


def _clean_name(raw: str | None) -> str:
    """«Никитин Алексей (селлер), Краснодар», «Дамир Сафин ММ6», «Сидоренко Роман Владимирович» → фамилия и имя."""
    t = re.sub(r"\(.*?\)", " ", raw or "")
    t = t.split(",")[0]
    words = [w for w in re.split(r"\s+", t) if w]
    words = [w for w in words if not re.fullmatch(r"(?i)мм\d+|mm\d+", w)
             and not re.search(r"(?i)(вич|вна|вны|ична|инична)$", w)]
    return " ".join(words)


def _match_engaged(conn, engaged: str | None) -> tuple[int | None, bool, list[int]]:
    """Кто вовлёк: @ник → по никам из чатов; иначе по имени. Одно имя, которое есть только
    у одного участника текущего потока, — тоже уверенно."""
    from . import posts
    cands = candidates(conn)
    raw = (engaged or "").strip()
    if raw.startswith("@") or re.fullmatch(r"[A-Za-z0-9_]{5,}", raw):
        nick = raw.lstrip("@").lower()
        for flow in {p["flow"] for p in cands if p["flow"] != "ментор"}:
            for pid, n in posts._nicks(conn, flow).items():
                if (n or "").lower() == nick:
                    return pid, True, [pid]
        for p in cands:
            if p["id"] < 0 and (_person(conn, p["id"]) or {}).get("nick", "") and _person(conn, p["id"])["nick"].lower() == nick:
                return p["id"], True, [p["id"]]
        return None, False, []
    pid, sure, near = _match(cands, _clean_name(raw))
    if pid and not sure and len(_words(_clean_name(raw))) == 1 and len(near) == 1:
        sure = True                     # одно имя, и оно только у одного участника или ментора
    elif pid and not sure and len(_words(_clean_name(raw))) >= 2 and len(near) == 1:
        sure = True                     # фамилия и имя совпали по первым буквам (Андроник / Андраник) и человек один
    return pid, sure, near


def poll_tilda(conn) -> int:
    """Новые заказы из чата Тильды. Первый запуск: последние 30 — «архив» (не публикуются сами)."""
    first = store.get_setting(conn, "sens:tilda_from") is None
    after = int(store.get_setting(conn, "sens:tilda_from", "0") or 0)
    msgs = _read(TILDA_CHAT, after, limit=30 if first else 60)
    new = 0
    for m in msgs:
        o = parse(m["text"])
        if o is None or m["out"]:
            continue
        if conn.execute("SELECT 1 FROM sens_orders WHERE msg_id=?", (m["id"],)).fetchone():
            continue
        pid, sure, near = _match_engaged(conn, o.get("engaged"))
        if first:
            status = "архив"
        elif o["kind"] == "заявка" or o["kind"] == "другое":
            status = "пропущено"
        elif o["kind"] == "безнал":
            status = "ждёт оплаты"
        else:
            status = "в очереди" if (pid and sure) else "кто вовлёк?"
        # повтор: этот же телефон уже оплачен/опубликован — второй новости не будет;
        # был запрос безнала, а потом оплатили картой — безнал закрываем
        ph = _digits(o.get("phone"))
        if ph and not first and o["kind"] in ("карта", "безнал"):
            prev = conn.execute("SELECT * FROM sens_orders WHERE phone_d=? ORDER BY id DESC", (ph,)).fetchall()
            if any(p["status"] in ("опубликовано", "в очереди", "публикуется") for p in prev):
                status = "повтор"
            elif o["kind"] == "карта":
                conn.execute("UPDATE sens_orders SET status='повтор', note='потом оплатили картой' "
                             "WHERE phone_d=? AND status='ждёт оплаты'", (ph,))
            elif o["kind"] == "безнал" and any(p["status"] == "ждёт оплаты" for p in prev):
                status = "повтор"
        conn.execute(
            "INSERT INTO sens_orders (msg_id, at, order_no, kind, amount, product, name, phone, phone_d, tg, city, "
            "engaged, person_id, sure, candidates, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (m["id"], m["date"], o.get("order_no"), o["kind"], o.get("amount"), o.get("product"), o.get("name"),
             o.get("phone"), ph, o.get("tg"), o.get("city"), o.get("engaged"), pid, int(sure),
             json.dumps(near), status))
        new += 1
    if msgs:
        store.set_setting(conn, "sens:tilda_from", str(max(m["id"] for m in msgs)))
    elif first:
        store.set_setting(conn, "sens:tilda_from", "0")
    return new


PAID = re.compile(r"оплат\w*\s+(при|пост|прошл|есть|получ)\w*|(при|пост)\w*\s+оплат\w*|деньги\s+(при|пост)\w*", re.I)
# «…, вовлёк Олег» / «вовлекла: Татьяна Орлова» — кто вовлёк, если в Тильде пусто или заказа нет
ENG = re.compile(r"(?:вовл[её]к\w*|вовлекл\w*|вовлечени\w*\s+от)\s*[:\-—–]?\s*([^\n,.;!]+)", re.I)


def is_paid_marker(text: str, forwarded: bool = False) -> bool:
    """«Оплата пришла …» — подтверждение: сообщение с этого начинается, короткое и не пересланное.
    Обсуждения («надо писать: оплата пришла…») и инструкции не срабатывают (урок 02.11)."""
    t = (text or "").strip()
    if forwarded or not t or len(t) > 150:
        return False
    m = PAID.search(t)
    return bool(m) and not re.sub(r"[\W\d_]+", "", t[:m.start()])


def _engaged_in(text: str) -> tuple[str | None, str]:
    """(кто вовлёк, текст без этой части)."""
    m = ENG.search(text or "")
    if not m:
        return None, text or ""
    return m.group(1).strip(), (text[:m.start()] + text[m.end():])


def _buyer_in(text: str) -> str:
    """Кто оплатил — что осталось от «Оплата пришла Белова Екатерина» без ключевых слов."""
    t = PAID.sub(" ", text or "")
    t = re.sub(r"(?i)\b(от|за|по|счёт|счет|заказ\w*|клиент\w*|участник\w*|безнал\w*)\b", " ", t)
    return " ".join(re.findall(r"[А-ЯЁA-Zа-яёa-z][а-яёa-z-]+", t)[:4])[:80]


def poll_bank(conn) -> int:
    """«Оплата пришла» в чате безнала → безнал-заказ становится оплаченным."""
    chat = int(setting(conn, "sens:bank_chat"))
    first = store.get_setting(conn, "sens:bank_from") is None
    after = int(store.get_setting(conn, "sens:bank_from", "0") or 0)
    msgs = _read(chat, after, limit=100)
    for m in msgs:
        conn.execute("INSERT OR IGNORE INTO sens_bank (msg_id, at, sender_id, sender, text, reply_to, marker) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (m["id"], m["date"], m["sender_id"], m["name"], m["text"], m["reply_to"],
                      int(is_paid_marker(m["text"], m.get("fwd", False)) and not first)))
    if msgs:
        store.set_setting(conn, "sens:bank_from", str(max(m["id"] for m in msgs)))
    elif first:
        store.set_setting(conn, "sens:bank_from", "0")
    done = 0
    for b in conn.execute("SELECT * FROM sens_bank WHERE marker=1 AND order_id IS NULL AND note IS NULL").fetchall():
        eng, _ = _engaged_in(b["text"])
        o, why = _bank_order(conn, b)
        if not o and eng:
            o, why = _order_without_tilda(conn, b, eng)
        if o and eng:
            pid, sure, near = _match_engaged(conn, eng)
            conn.execute("UPDATE sens_orders SET engaged=?, person_id=?, sure=?, candidates=? WHERE id=?",
                         (eng, pid, int(sure), json.dumps(near), o["id"]))
            o = conn.execute("SELECT * FROM sens_orders WHERE id=?", (o["id"],)).fetchone()
        if o:
            conn.execute("UPDATE sens_bank SET order_id=? WHERE msg_id=?", (o["id"], b["msg_id"]))
            conn.execute("UPDATE sens_orders SET paid_at=?, paid_note=?, status=? WHERE id=?",
                         (b["at"], f"«{(b['text'] or '')[:80]}» — {b['sender']}",
                          "в очереди" if (o["person_id"] and o["sure"]) else "кто вовлёк?", o["id"]))
            done += 1
        else:
            conn.execute("UPDATE sens_bank SET note=? WHERE msg_id=?", (why, b["msg_id"]))
    return done


def _order_without_tilda(conn, b: sqlite3.Row, eng: str) -> tuple[sqlite3.Row | None, str]:
    """Счёт выставили в личке, заказа в Тильде нет: «Оплата пришла Иванов Пётр, вовлёк Олег» — заводим сами."""
    buyer = _buyer_in(_engaged_in(b["text"])[1])
    if len(_words(buyer)) < 1:
        return None, "заказа в Тильде нет — напишите и кто оплатил, и кто вовлёк"
    key = set(_words(buyer))
    for o in conn.execute("SELECT * FROM sens_orders WHERE status IN ('опубликовано', 'в очереди', 'публикуется')"):
        if key and key <= set(_words(o["name"])):
            return None, f"про {o['name']} новость уже была"
    conn.execute(
        "INSERT INTO sens_orders (msg_id, at, kind, name, engaged, status, note) VALUES (?,?,?,?,?,?,?)",
        (-b["msg_id"], b["at"], "безнал", buyer, eng, "кто вовлёк?", "без заказа в Тильде — из чата безнала"))
    return conn.execute("SELECT * FROM sens_orders WHERE msg_id=?", (-b["msg_id"],)).fetchone(), ""


def _waiting(conn) -> list[sqlite3.Row]:
    """Безнал-заказы, которые ждут оплаты: новые и старые (до включения), если тот же телефон
    потом не оплатил картой; один человек мог запросить счёт дважды — берём свежий запрос."""
    rows = conn.execute(
        "SELECT * FROM sens_orders o WHERE o.kind='безнал' AND (o.status='ждёт оплаты' OR (o.status='архив' "
        "AND NOT EXISTS (SELECT 1 FROM sens_orders k WHERE k.phone_d=o.phone_d AND k.kind='карта' AND k.id<>o.id) "
        "AND NOT EXISTS (SELECT 1 FROM sens_orders k WHERE k.phone_d=o.phone_d AND k.paid_at IS NOT NULL))) "
        "ORDER BY o.msg_id DESC").fetchall()
    seen, out = set(), []
    for o in rows:
        if o["phone_d"] and o["phone_d"] in seen:
            continue
        seen.add(o["phone_d"])
        out.append(o)
    return out


def _find(wait: list[sqlite3.Row], text: str) -> list[sqlite3.Row]:
    """Заказы, на которые указывает текст: номер заказа, телефон, фамилия и имя (или редкая фамилия)."""
    nums = set(re.findall(r"\d{6,}", text))
    phones = {re.sub(r"\D", "", x)[-10:] for x in re.findall(r"[+\d][\d\s()-]{9,}", text)}
    hit = [o for o in wait if (o["order_no"] and o["order_no"] in nums) or (o["phone_d"] and o["phone_d"] in phones)]
    if hit or parse(text):              # пересланный заказ Тильды — только по номеру и телефону, не по словам
        return hit
    words = set(_words(text))
    return [o for o in wait if sum(x in words for x in _words(o["name"])) >= 2]


def _bank_order(conn, b: sqlite3.Row) -> tuple[sqlite3.Row | None, str]:
    """Какой заказ оплачен. Сначала — само сообщение. Только если в нём нет ничего, кроме «оплата пришла»,
    смотрим, на что оно ответ, и ближайшее соседнее сообщение того же человека (±3 мин) — пересланный заказ.
    Другие «оплата пришла» соседями не считаются: подряд две оплаты не путаются."""
    wait = _waiting(conn)
    own = _engaged_in(b["text"] or "")[1]
    says = bool(re.search(r"\d{6,}", own)) or bool(_words(_buyer_in(own)))
    sources = [own]
    if not says:
        if b["reply_to"]:
            r = conn.execute("SELECT text FROM sens_bank WHERE msg_id=?", (b["reply_to"],)).fetchone()
            if r:
                sources.append(r["text"] or "")
        at = datetime.fromisoformat(b["at"])
        near = sorted((abs((datetime.fromisoformat(r["at"]) - at).total_seconds()), r["text"] or "")
                      for r in conn.execute("SELECT * FROM sens_bank WHERE sender_id=? AND msg_id<>? AND marker=0",
                                            (b["sender_id"], b["msg_id"])))
        sources += [t for d, t in near if d <= 180][:1]
    if not wait:
        return None, "нет заказов, которые ждут безнала"
    for src in sources:
        hit = _find(wait, src)
        if len(hit) == 1:
            return hit[0], ""
        if len(hit) > 1:
            return None, "подходит несколько заказов: " + ", ".join(o["name"] or "?" for o in hit[:4])
        # заказ уже отмечен оплаченным или новость про него вышла
        o = parse(src)
        num = (o or {}).get("order_no") or next(iter(re.findall(r"\d{6,}", src)), None)
        if num:
            done = conn.execute("SELECT * FROM sens_orders WHERE (order_no=? OR phone_d=?) AND (paid_at IS NOT NULL "
                                "OR status IN ('опубликовано', 'в очереди', 'публикуется'))", (num, num[-10:])).fetchone()
            if done:
                return None, f"заказ {done['name']} уже отмечен — второй новости не будет"
    return None, "не понял, чей заказ — перешлите заказ из Тильды или напишите фамилию и имя"


# ----------------------------------------------------------------- публикация

def _flow(conn) -> sqlite3.Row | None:
    fid = setting(conn, "sens:flow")
    return flows.get(conn, int(fid)) if fid.isdigit() else flows.current(conn)


# ----------------------------------------------------------------- текст поста

# Промпт собран по 49 настоящим «Сенсационным новостям» Марьи в чате ММ7 (30.09–02.11.2026):
# про деньги и оплату там нет ни слова, общий счётчик — только по круглым числам,
# зато почти всегда — личный порядковый номер вовлечения и образ под человека.
SENS_PROMPT = """Ты — Марья, организатор мастермайнда «Вершина». Пишешь {where} «Сенсационную новость»: {who_kind} только что привёл(а) нового человека — у него/неё +1 вовлечение. Задача поста — публично, тепло и с юмором порадоваться за человека и зарядить команду.

Строение (как в настоящих постах):
1. Первая строка ровно: 🔥СЕНСАЦИОННЫЕ НОВОСТИ🔥
2. Пустая строка, затем ровно: {mention} +1 вовлечение!
3. Пустая строка, затем 2–4 коротких абзаца по 1–3 предложения:
   - зацепка: один образ или игра на одну тему — охота и трофеи, подкаст «Приём, приём!», сказка, космос и ракета, рыцари и принцессы, гонка, марафон, народная мудрость, «сказано — сделано», «девушки в прайме» / «мужская позиция» и т. п. Тему бери прежде всего от человека: его прозвища и характера из прошлых постов про него, ника, свежего дела из его сообщений. Время суток и день недели — лишь изредка («Спать сегодня не уходим», «Кто не отдыхает в выходной…»), не начинай с них по умолчанию и не называй часы. Не бери тему, которая была в одном из трёх последних постов чата;
   - какое это вовлечение у человека: первое — «поздравляем с первым вовлечением!», дальше — «ВТОРОЕ», «уже 4-е», «Итого 3!» (порядковое часто капсом). Номер — только из фактов; если в фактах номера нет — пиши «ещё одно вовлечение», «снова в деле», без числа;
   - похвала качеству, которое за этим стоит (держит слово, упорство, тепло к людям, темп, командность) — на «ты», по-свойски. Если в прошлых постах про человека было ласковое или короткое имя (Маша, Лёша, Рома, Надя, Ириночка, дядя Гриша) — зови так же;
4. Последний абзац — короткий финал, каждый раз свой: «Имя, так держать! 👏», «мы тобой гордимся!», «продолжаем в том же духе!», «до новых космических результатов!», изредка — вопрос команде. Не повторяй финал трёх последних постов чата.

Тон: восторженный, живой, разговорный, без пафоса и канцелярита. Эмодзи — 3–7 на весь пост, в конце фраз (🔥 🚀 💪 ❤️ 👏 ✨ 🏆 🎯 😍). Длина — 350–800 знаков.

Нельзя:
- писать про оплату, деньги, сумму, счёт, заказ, Тильду, «пришла оплата», цену, Казань и даты мероприятия — новость про человека, а не про сделку;
- называть нового человека (имя, город, бизнес) — он просто «новый участник», можно вообще не упоминать;
- писать общий счётчик потока («вовлекли N из M») — только если в фактах есть строка «Круглое число», тогда одной фразой в духе «50 вовлечений на потоке — серьёзная отметка!»;
- путать номер вовлечения у человека с числом новостей за сегодня — это разные вещи;
- придумывать факты, числа и события; деталь из сообщений человека — только лёгкая (спорт, баня, созвон с командой, поездка, настроение) и только если она есть ниже; никогда — здоровье, семья и дети, отношения, деньги, долги, проблемы, терапия, работа клиентов;
- повторять дословно шутки из прошлых постов;
- markdown (звёздочки, решётки), хэштеги, кавычки вокруг поста, подпись.

Ответь только текстом поста.

Последние сенсационные новости в чате потока (свежие сверху) — образец стиля:
{recent}

Прошлые новости про этого человека (свежие сверху):
{own}

Факты:
{facts}
"""

_ORD = {"перв": 1, "втор": 2, "трет": 3, "четв": 4, "пят": 5, "шест": 6, "седьм": 7, "восьм": 8,
        "девят": 9, "десят": 10}
_RECENT: dict[int, tuple[float, list[dict]]] = {}


def _sens_chat_of_flow(conn, f) -> int | None:
    """Чат потока, куда Марья пишет сенсационные новости (где их больше всего)."""
    ids = [c["chat_id"] for c in flows.chats(conn, f["id"]) if c["kind"] == "поток"]
    if not ids:
        return None
    best = conn.execute(
        f"SELECT chat_id, COUNT(*) n FROM chat_messages WHERE chat_id IN ({','.join('?' * len(ids))}) "
        "AND upper(text) LIKE '%СЕНСАЦИОН%' GROUP BY chat_id ORDER BY n DESC LIMIT 1", ids).fetchone()
    return best["chat_id"] if best else ids[0]


def recent_posts(conn, f) -> list[dict]:
    """Сенсационные новости чата потока за 3 недели, свежие сверху (база + дочитать из телеграма)."""
    chat = _sens_chat_of_flow(conn, f)
    if not chat:
        return []
    hit = _RECENT.get(chat)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    since = datetime.now(timezone.utc) - timedelta(days=21)
    rows = {r["msg_id"]: {"at": r["at"], "text": r["text"]} for r in conn.execute(
        "SELECT msg_id, at, text FROM chat_messages WHERE chat_id=? AND at>=? AND upper(text) LIKE '%СЕНСАЦИОН%'",
        (chat, since.isoformat()))}
    try:
        # поиском телеграма — один запрос вместо чтения всей переписки за дни
        res = tg_account.search_messages(chat, "СЕНСАЦИОННЫЕ", limit=40)
        for m in res.get("messages") or []:
            if "СЕНСАЦИОН" in (m["text"] or "").upper() and m["date"] >= since.isoformat():
                rows[m["id"]] = {"at": m["date"], "text": m["text"]}
    except Exception:                     # noqa: BLE001 — хватит и того, что в базе
        pass
    out = [rows[k] for k in sorted(rows, reverse=True)]
    _RECENT[chat] = (time.time(), out)
    return out


def _ordinal_in(text: str) -> int | None:
    """«ПЯТОЕ вовлечение», «уже 4-е», «с первым вовлечением», «Итого 3!» → число."""
    t = re.sub(r"вовлеч\w*\s+(за\s+(один|сегодняшний)\s+день|за\s+сегодня|за\s+день)", "за день", text.lower())
    m = re.search(r"(\d+)\s*-?\s*(?:е|ое|ье|й)\s+вовлеч|итого\s+(\d+)", t)
    if m:
        return int(m.group(1) or m.group(2))
    for stem, n in _ORD.items():
        if re.search(rf"\b{stem}\w*\s+вовлеч", t):
            return n
    return None


def ordinal(conn, o, p: dict | None, posts_own: list[dict]) -> int | None:
    """Какое это вовлечение у человека. Два источника: таблица команды и номера в постах Марьи.
    Сходятся — номер; первый раз — 1; расходятся — None (в посте без числа)."""
    by_posts = None
    for i, post in enumerate(posts_own):            # свежие сверху
        n = _ordinal_in(post["text"])
        if n:
            by_posts = n + i + 1                    # после того поста были ещё i новостей
            break
    by_table = None
    if p and p.get("card") is not None:
        rows = conn.execute("SELECT text FROM involved WHERE person_id=? AND done=1", (-p["card"],)).fetchall()
        day = (o["paid_at"] or o["at"] or "")[:10]
        dm = f"{day[8:10]}.{day[5:7]}" if len(day) == 10 else "??"
        already = any(dm in (r["text"] or "") for r in rows)
        by_table = len(rows) + (0 if already else 1)
    if not posts_own and (by_table in (None, 1)):
        return 1
    if by_posts and by_table:
        return by_posts if by_posts == by_table else None
    return by_posts if not p or p.get("card") is None else None


_DAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")


def facts(conn, o, p: dict | None, n: int | None, count: int, goal: int, today_n: int) -> str:
    now = datetime.now(MSK)
    part = "ночь" if now.hour < 6 else "утро" if now.hour < 12 else "день" if now.hour < 18 else "вечер"
    lines = []
    if p:
        team = p["team"]
        team = "" if not team or re.fullmatch(r"(?i)(команда|группа)\s*\d*|без команды", team) else f", группа «{team}»"
        role = f"участник(ца) потока {p['flow']}{team}" if p["card"] is not None else "ментор потока"
        lines.append(f"Кто вовлёк: {p['name']}, {role}.")
    lines.append(f"Какое это вовлечение у человека: {n}." if n else
                 "Какое это вовлечение у человека: номер неизвестен — без числа.")
    if today_n >= 3:
        lines.append(f"Сегодня в чате это уже {today_n}-я сенсационная новость (не номер человека; упоминать не обязательно).")
    mentor = bool(p and p["card"] is None)
    if mentor:
        lines.append("Это вовлечение ментора: новость идёт в чат менторов как мотивационная; в копилку потока оно не идёт — "
                     "общий счёт вовлечений потока не упоминай.")
        if count and count % 10 == 0:
            lines.append(f"Круглое число: менторы вовлекли уже {count}.")
    elif goal and count == goal:
        lines.append(f"Круглое число: этим вовлечением поток взял планку — {count} из {goal}!")
    elif count and count % 10 == 0:
        lines.append(f"Круглое число: вовлечений на потоке стало {count}.")
    lines.append(f"Сейчас: {_DAYS[now.weekday()]}, {part} (по Москве).")
    msgs = []
    if p:
        tg = p.get("tg")
        if p.get("card") is not None:
            r = conn.execute("SELECT sender_id FROM chat_senders WHERE card=? LIMIT 1", (p["card"],)).fetchone()
            tg = r["sender_id"] if r else None
        if tg:
            since = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
            msgs = conn.execute("SELECT at, text FROM chat_messages WHERE sender_id=? AND at>=? AND length(text)>20 "
                                "ORDER BY at DESC LIMIT 3", (tg, since)).fetchall()
    if msgs:
        lines.append("Свежие сообщения человека в чатах потока (для лёгкой детали, если подходит):")
        lines += [f"- {m['at'][:10]}: {(m['text'] or '')[:300]}" for m in msgs]
    return "\n".join(lines)


def compose(conn, o, count: int, goal: int) -> str:
    """Текст новости от Claude по образцу постов Марьи."""
    import subprocess
    from . import claude_env
    f = _flow(conn)
    p = _person(conn, o["person_id"]) if o["person_id"] else None
    mention = (f"@{p['nick']}" if p and p["nick"] else p["name"] if p else "Новый участник")
    recent = recent_posts(conn, f)
    own = [r for r in recent if p and p["nick"] and ("@" + p["nick"]).lower() in r["text"].lower()] if p else []
    if p and not own:                        # без ника — по имени в первых строках
        first = p["name"].split()[0].lower()
        own = [r for r in recent if first in r["text"][:200].lower()]
    today = datetime.now(MSK).date().isoformat()
    # новости за сегодня — по ленте чата потока (наши, когда пойдут туда, тоже в ней)
    today_n = 1 + sum(1 for r in recent if datetime.fromisoformat(r["at"]).astimezone(MSK).date().isoformat() == today)
    n = ordinal(conn, o, p, own)
    shown = "\n\n".join(f"--- {r['at'][:10]} ---\n{r['text'].strip()}" for r in recent[:12]) or "(нет)"
    mine = "\n\n".join(f"--- {r['at'][:10]} ---\n{r['text'].strip()}" for r in own[:4]) or "(про этого человека новостей ещё не было)"
    mentor = bool(p and p["card"] is None)
    prompt = SENS_PROMPT.format(where=f"в чат менторов потока {f['name']}" if mentor else f"в общий чат потока {f['name']}",
                                who_kind="ментор" if mentor else "участник",
                                mention=mention, recent=shown, own=mine,
                                facts=facts(conn, o, p, n, count, goal, today_n))
    if not claude_env.token():
        raise RuntimeError(claude_env.NOT_LOGGED)
    done = subprocess.run(["claude", "-p"], input=prompt, capture_output=True, text=True,
                          timeout=180, env=claude_env.env(), cwd="/tmp")
    out = (done.stdout or "").strip()
    if done.returncode != 0 or not out or "not logged in" in out.lower():
        raise RuntimeError(claude_env.explain((done.stderr or out or "пустой ответ").strip()[:300]))
    return out


def _fallback_text(conn, o) -> str:
    p = _person(conn, o["person_id"]) if o["person_id"] else None
    who = (f"@{p['nick']}" if p["nick"] else p["name"]) if p else "Новый участник"
    name = p["name"].split()[0] if p else ""
    return f"🔥СЕНСАЦИОННЫЕ НОВОСТИ🔥\n\n{who} +1 вовлечение!\n\n" + (f"{name}, так держать! 🔥🚀" if name else "Так держать! 🔥🚀")


def publish(conn, order_id: int, who: str = "платформа") -> str:
    """Пост + фото чата. Возвращает, что получилось (для страницы)."""
    from . import posts, sens_avatar
    from .proclock import held
    with _RUN, held("sensation"):
        pre = conn.execute("SELECT person_id FROM sens_orders WHERE id=?", (order_id,)).fetchone()
        mentor = bool(pre and (pre["person_id"] or 0) < 0)          # id < 0 — ментор (минус id учётки)
        chat = int(setting(conn, "sens:mentor_chat" if mentor else "sens:chat"))
        if not tg_account.sending_allowed(chat):
            conn.execute("UPDATE sens_orders SET status='ошибка', note=? WHERE id=? AND status NOT IN ('опубликовано', 'публикуется')",
                         ("замок отправки закрыт для этого чата", order_id))
            return "Замок отправки закрыт для этого чата: «Управление» → «Телеграм»."
        # захват: процессов несколько — пост уходит один раз
        took = conn.execute("UPDATE sens_orders SET status='публикуется', note=NULL WHERE id=? "
                            "AND status NOT IN ('опубликовано', 'публикуется')", (order_id,)).rowcount
        if not took:
            return "Уже опубликовано или публикуется."
        o = conn.execute("SELECT * FROM sens_orders WHERE id=?", (order_id,)).fetchone()
        ckey = "sens:mentor_count" if mentor else "sens:count"
        count = int(setting(conn, ckey) or 0) + 1
        goal = 0 if mentor else int(setting(conn, "sens:goal") or 0)
        f = _flow(conn)
        try:
            text = compose(conn, o, count, goal)
            note = None
        except Exception as exc:          # noqa: BLE001 — без Claude всё равно публикуем простым текстом
            text, note = _fallback_text(conn, o), f"Claude не ответил ({str(exc)[:120]}) — простой текст"
        files = []
        if o["person_id"]:
            try:
                files = [_photo(conn, o["person_id"])]
            except Exception as exc:      # noqa: BLE001
                note = (note + "; " if note else "") + f"без фото: {exc}"
        res = tg_account.send(chat, text, files=files or None)
        if not res.get("ok"):
            conn.execute("UPDATE sens_orders SET status='ошибка', note=? WHERE id=?",
                         (res.get("error") or "не отправилось", order_id))
            return "Не отправилось: " + (res.get("error") or "")
        title = next((c["title"] for c in (tg_account.chats().get("chats") or []) if c["id"] == chat), str(chat))
        cur = conn.execute(
            "INSERT INTO posts (flow_id, kind, chat_id, topic_id, chat_title, text, status, note, created_by, created_at, "
            "updated_at, sent_by, sent_at, msg_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f["id"], "сенсационная новость", chat, 0, title, text, "отправлен",
             f"сама по оплате из Тильды (заказ {o['order_no']})", who, _now(), _now(), who, _now(), res["msg_id"]))
        store.set_setting(conn, ckey, str(count))
        if not mentor and setting(conn, "sens:avatar") == "1":            # вовлечения менторов фото не меняют
            pchat = int(setting(conn, "sens:photo_chat"))
            path = posts.MEDIA / f"sens_avatar_{count}.jpg"
            posts.MEDIA.mkdir(parents=True, exist_ok=True)
            path.write_bytes(sens_avatar.render(count, goal))
            r = tg_account.set_chat_photo(pchat, str(path))
            if not r.get("ok"):
                note = (note + "; " if note else "") + "фото чата не сменилось: " + (r.get("error") or "")
        conn.execute("UPDATE sens_orders SET status='опубликовано', post_id=?, count_after=?, note=?, decided_by=? WHERE id=?",
                     (cur.lastrowid, count, note, who, order_id))
        what = f"в чат менторов, менторы вовлекли {count}" if mentor else f"вовлекли {count}"
        return f"Опубликовано, {what}." + (f" ({note})" if note else "")


def publish_async(order_id: int, who: str) -> None:
    def run():
        c = store.connect()
        try:
            publish(c, order_id, who)
        except Exception as exc:          # noqa: BLE001
            c.execute("UPDATE sens_orders SET status='ошибка', note=? WHERE id=?", (str(exc)[:300], order_id))
        finally:
            c.close()
    threading.Thread(target=run, daemon=True).start()


def follow_migrations(conn) -> list[str]:
    """Группа стала супергруппой — переключиться на её новый номер (02.11 так переехал чат безнала).
    В новой супергруппе свой счёт сообщений — читаем её с начала."""
    done = []
    for key in ("sens:bank_chat", "sens:chat", "sens:photo_chat", "sens:mentor_chat"):
        old = int(setting(conn, key))
        new = tg_account.migrated_to(old)
        if not new:
            continue
        store.set_setting(conn, key, str(new))
        if key == "sens:bank_chat":
            store.set_setting(conn, "sens:bank_from", "0")
        if key != "sens:bank_chat":
            # замок: новый номер тестового чата тоже пускаем, если пускали старый
            raw = store.get_setting(conn, "tg:test_chats", "") or ""
            if str(old) in raw.split() and str(new) not in raw.split():
                store.set_setting(conn, "tg:test_chats", (raw + " " + str(new)).strip())
        done.append(f"{key}: {old} → {new}")
    return done


_LAST_MIGR = [0.0]


def tick(conn) -> None:
    """Раз в минуту: дочитать Тильду и безнал; если включено — опубликовать готовое."""
    if not tg_account.configured():
        return
    if time.time() - _LAST_MIGR[0] > 600:          # переезд групп — раз в 10 минут
        _LAST_MIGR[0] = time.time()
        for line in follow_migrations(conn):
            print("sensation: чат переехал", line, flush=True)
    poll_tilda(conn)
    poll_bank(conn)
    try:                                   # оплаты → строки таблицы потока
        from . import rows
        rows.pull(conn)
    except Exception as e:
        print("rows: оплаты не разобрались:", e, flush=True)
    if setting(conn, "sens:on") != "1":
        return
    for o in conn.execute("SELECT id FROM sens_orders WHERE status='в очереди' ORDER BY id").fetchall():
        publish(conn, o["id"])


def start_background() -> None:
    """Только в процессе-лидере, один раз при старте приложения."""
    c = store.connect()
    try:
        init(c)
        # перезапуск посреди публикации: не висеть в «публикуется» — человек проверит чат и решит
        c.execute("UPDATE sens_orders SET status='ошибка', note='прервано перезапуском — проверьте чат, "
                  "прежде чем публиковать снова' WHERE status='публикуется'")
    finally:
        c.close()

    def loop():
        time.sleep(20)
        while True:
            c = store.connect()
            try:
                init(c)
                tick(c)
            except Exception as exc:          # noqa: BLE001 — фоновая задача не должна падать
                print("sensation:", exc, flush=True)
            finally:
                c.close()
            time.sleep(60)
    threading.Thread(target=loop, daemon=True, name="sensation").start()


# ----------------------------------------------------------------- страница

def decide(conn, order_id: int, act: str, pid: str, who: str) -> str:
    o = conn.execute("SELECT * FROM sens_orders WHERE id=?", (order_id,)).fetchone()
    if not o:
        return "Заказа нет."
    if act == "skip":
        conn.execute("UPDATE sens_orders SET status='пропущено', decided_by=? WHERE id=?", (who, order_id))
        return "Пропущено — новости не будет."
    if act == "paid":
        conn.execute("UPDATE sens_orders SET paid_at=?, paid_note=?, status=? WHERE id=?",
                     (_now(), f"отмечено на платформе — {who}",
                      "в очереди" if (o["person_id"] and o["sure"]) else "кто вовлёк?", order_id))
        return "Отмечено: оплата пришла."
    if act == "publish":
        if o["status"] in ("опубликовано", "публикуется"):
            return "Уже опубликовано."
        # число — выбрал человек (ментор — отрицательное); «none» — без вовлёкшего; пусто — как нашла платформа
        if pid.lstrip("-").isdigit() and int(pid):
            conn.execute("UPDATE sens_orders SET person_id=?, sure=1 WHERE id=?", (int(pid), order_id))
        elif pid == "none":
            conn.execute("UPDATE sens_orders SET person_id=NULL, sure=0 WHERE id=?", (order_id,))
        conn.execute("UPDATE sens_orders SET decided_by=? WHERE id=?", (who, order_id))
        publish_async(order_id, who)
        return "Публикуется — через минуту обновите страницу."
    return "Непонятное действие."


def listing(conn) -> dict[str, Any]:
    people = {p["id"]: p for p in candidates(conn)}
    rows = []
    for o in conn.execute("SELECT * FROM sens_orders ORDER BY msg_id DESC LIMIT 80").fetchall():
        d = dict(o)
        d["who"] = people.get(o["person_id"])
        d["near"] = [people[i] for i in json.loads(o["candidates"] or "[]") if i in people]
        rows.append(d)
    bank = conn.execute("SELECT * FROM sens_bank WHERE marker=1 ORDER BY msg_id DESC LIMIT 20").fetchall()
    known = {c["id"]: c["title"] for c in (tg_account.chats().get("chats") or [])}
    return {"orders": rows, "bank": bank, "people": list(people.values()), "known": known,
            "settings": {k: setting(conn, k) for k in DEFAULTS}}
