"""«Финансы»: деньги сообщества из чата «Финансы ММ» — своё подобие Компаса
внутри платформы.

В чате три вида сообщений: поступления (Полина: «+99 000 руб., ФИО, ММ8
Казань, вовлек X»), заявки на расход («Проект / Подпроект / СР / Сумма / куда»)
и ответы Татьяны с платёжкой — это отметка «оплачено». Остальное — обсуждения.

Claude два раза в день разбирает новые сообщения пачкой и раскладывает их
в операции: приход или расход, проект, подпроект, статья, кошелёк, статус.
Что непонятно — не угадывает, а ставит вопрос с вариантами; человек выбирает
одним нажатием, и это решение уходит в примеры для следующих разборов.
Операцию, которую поправил человек, Claude больше не трогает.

Отсюда: ДДС по статьям и месяцам, итоги по проектам, «не оплачено»,
остатки по кошелькам (счёт, карты, наличные). Видно только администратору.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from . import claude_env, store

MSK = timezone(timedelta(hours=3))
RUN_HOURS = (13, 21)          # разбор по расписанию, МСК
BATCH = 45                    # сообщений на один запрос Claude
PAYER = "finance_payer"           # Татьяна: её ответ с платёжкой = «оплачено»

KINDS = ("приход", "расход", "перевод")
STATUSES = ("заявка", "оплачено", "поступило", "отменено")
MOVED = ("оплачено", "поступило")     # деньги реально прошли
FIELDS = ("kind", "amount", "project", "subproject", "article", "counterparty", "purpose",
          "wallet", "participant", "engaged_by", "status", "note", "at")

DEFAULT_DICT = {
    "проект": ["ММ7", "ММ8 Казань", "4-дневный мастермайнд", "ММ5", "ММ6", "Вне ММ"],
    "подпроект": ["Переменные расходы", "Постоянные расходы", "Экватор", "Разборы", "Вовлечение",
                  "Командировки"],
    "статья расхода": ["Вовлечение", "Дорога и билеты", "Проживание", "Командировочные",
                       "Мерч", "Дизайн", "Реклама", "Зарплата", "Трекеры и менторы", "Хелперы",
                       "Техническое обеспечение", "Налоги", "Юридические услуги",
                       "Подбор персонала", "Подписки и сервисы", "Связь", "Аренда площадки",
                       "Оборудование и реквизит", "Питание", "Ведущие и эксперты",
                       "Фото и видео", "Представительские", "Доставка", "Возврат участнику",
                       "Прочие расходы"],
    "статья прихода": ["Оплата участия", "Предоплата участия", "Штраф", "Цена слова",
                       "Прочие поступления"],
    "кошелёк": ["Р/с Точка", "Карта Озон", "Наличные Лилии", "Карта Марьи", "Эквайринг сайта"],
}


def init(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS fin_ops (
        id INTEGER PRIMARY KEY,
        chat_id INTEGER, msg_id INTEGER, part INTEGER NOT NULL DEFAULT 0,
        at TEXT, author TEXT,
        kind TEXT NOT NULL DEFAULT 'расход', amount REAL NOT NULL DEFAULT 0,
        project TEXT, subproject TEXT, article TEXT,
        counterparty TEXT, purpose TEXT, wallet TEXT,
        participant TEXT, engaged_by TEXT,
        status TEXT NOT NULL DEFAULT 'заявка',
        paid_msg_id INTEGER, paid_at TEXT, paid_note TEXT,
        question TEXT, options TEXT, confidence REAL,
        source TEXT NOT NULL DEFAULT 'claude',
        edited_by TEXT, edited_at TEXT, note TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (chat_id, msg_id, part)
    );
    CREATE TABLE IF NOT EXISTS fin_seen (
        chat_id INTEGER, msg_id INTEGER, hash TEXT, at TEXT,
        PRIMARY KEY (chat_id, msg_id)
    );
    CREATE TABLE IF NOT EXISTS fin_runs (
        id INTEGER PRIMARY KEY, started TEXT, finished TEXT, status TEXT, note TEXT,
        msgs INTEGER DEFAULT 0, ops INTEGER DEFAULT 0, slot TEXT
    );
    CREATE TABLE IF NOT EXISTS fin_dict (
        kind TEXT, name TEXT, pos INTEGER, PRIMARY KEY (kind, name)
    );
    CREATE TABLE IF NOT EXISTS fin_wallets (
        name TEXT PRIMARY KEY, start_amount REAL NOT NULL DEFAULT 0, start_date TEXT
    );
    """)
    if not conn.execute("SELECT 1 FROM fin_dict LIMIT 1").fetchone():
        for kind, names in DEFAULT_DICT.items():
            for i, n in enumerate(names):
                conn.execute("INSERT INTO fin_dict VALUES (?,?,?)", (kind, n, i))


# ----------------------------------------------------------------- справочник

def dictionary(conn) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {k: [] for k in DEFAULT_DICT}
    for r in conn.execute("SELECT kind, name FROM fin_dict ORDER BY kind, pos, name"):
        out.setdefault(r["kind"], []).append(r["name"])
    return out


def save_dictionary(conn, kind: str, text: str) -> None:
    if kind not in DEFAULT_DICT:
        raise ValueError("Нет такого справочника.")
    names = []
    for line in (text or "").splitlines():
        n = line.strip()
        if n and n not in names:
            names.append(n[:80])
    conn.execute("DELETE FROM fin_dict WHERE kind=?", (kind,))
    for i, n in enumerate(names):
        conn.execute("INSERT INTO fin_dict VALUES (?,?,?)", (kind, n, i))


def chats(conn) -> list[sqlite3.Row]:
    """Чаты с чеками — отмечены в «Потоках» видом «чеки»."""
    return conn.execute("SELECT chat_id, MIN(title) AS title FROM flow_chats WHERE kind='чеки' "
                        "GROUP BY chat_id").fetchall()


def msg_link(chat_id: int, msg_id: int | None) -> str | None:
    if not msg_id:
        return None
    s = str(chat_id)
    return f"https://t.me/c/{s[4:]}/{msg_id}" if s.startswith("-100") else None


# ----------------------------------------------------------------- разбор Claude

PROMPT = """Ты — финансист сообщества «Вершина» (мастермайнды для предпринимателей). Ниже — новые
сообщения из рабочего чата «Финансы ММ». Разложи их в операции учёта денег.

Как устроен чат:
- Поступления пишет Полина: «@finance_payer, +99 000 руб., Кузнецова Анна, ММ8 Казань, вовлек Сидоренко
  Роман» — это приход, статус «поступило», статья «Оплата участия» (или «Предоплата участия», если
  сказано «предоплата»), participant — кто оплатил, engaged_by — кто вовлёк. Штраф участника — приход,
  статья «Штраф».
- Заявки на расход: «Проект / Подпроект / СР (статья расходов) / Сумма / куда платить», обычно с
  @finance_payer — это расход со статусом «заявка» (ещё не оплачен). Если автор пишет, что уже оплатил
  сам («списать с моих наличных», «оплата с карты из суммы за штрафы», «оплачено из наличных») —
  статус «оплачено» и кошелёк, откуда ушли деньги. Во всех остальных случаях расход — «заявка», даже
  если видишь ответ с платёжкой: оплату отмечай только через payments.
- Татьяна Смирнова (@finance_payer) отвечает на заявку фото или файлом платёжки — это оплата заявки,
  запиши в payments. Текстовые «оплачено», «готово», «перевела» в ответ на заявку — тоже payments.
- Обсуждения, вопросы, планы оплат («на неделю планируются оплаты…»), благодарности — не операции.
- В одном сообщении бывает несколько расходов (разные проекты, разные статьи, разные люди) — сделай
  несколько операций с part 0, 1, 2… Сумма «Итого» — не отдельная операция.
- Если сообщение уточняет или пересчитывает уже существующую операцию (список «Открытые операции»),
  — не заводи новую, а дай updates для неё. Если операция отменена — cancel.
- «перевод» — деньги переходят между своими кошельками (сняли наличные со счёта, Лилия перевела со
  своего остатка) — это не доход и не расход.

Проект, подпроект, статья и кошелёк — СТРОГО из справочника ниже (точное написание). Если ничего не
подходит — ближайшее и вопрос. В чате пишут вольно: «мм 7» = «ММ7», «Ср вовлечение» = статья
«Вовлечение», «СР: перелеты эксперты» = «Дорога и билеты»,
возврат денег участнику — расход, статья «Возврат участнику», «мм 8 Казань» = «ММ8 Казань».
Подпроект «Переменные расходы», если в заявке так и написано или подпроект не назван у обычного
расхода потока.

Вопрос (question) — только когда правда непонятно, к чему отнести: нет проекта, проект плавает,
непонятно кому, сумма не сходится. Тогда в options — варианты по полям (не больше 4 на поле),
например {{"project": ["ММ7", "ММ8 Казань"]}}. Если понятно — question = null, options = {{}}.
confidence — насколько уверен в разноске (0..1). Если меньше 0,7 — вопрос обязателен.

Справочник:
{dictionary}

Как раньше решали люди (учись на этом):
{examples}

Открытые операции (ещё не оплачены или недавние, на них могут ссылаться ответы):
{open_ops}

Сообщения (формат: [номер] дата время Автор (@ник) ↩номер_на_что_ответ [вложение]: текст):
{messages}

Ответ — только JSON, без пояснений:
{{"ops": [{{"msg": 123, "part": 0, "kind": "расход|приход|перевод", "amount": 12000,
  "project": "...", "subproject": "... или null", "article": "...", "counterparty": "кому / от кого",
  "purpose": "коротко, за что", "wallet": "кошелёк или null", "participant": "ФИО или null",
  "engaged_by": "ФИО или null", "status": "заявка|оплачено|поступило", "confidence": 0.9,
  "question": null, "options": {{}}}}],
 "payments": [{{"msg": 130, "request_msg": 123, "part": null, "wallet": "кошелёк или null",
  "note": "коротко"}}],
 "updates": [{{"op": 17, "fields": {{"amount": 18662}}, "why": "коротко"}}],
 "cancel": [{{"op": 18, "why": "коротко"}}]}}
"""


def _hash(r) -> str:
    return hashlib.sha1(f"{r['text'] or ''}|{r['media'] or ''}".encode()).hexdigest()[:16]


def pending(conn) -> list[sqlite3.Row]:
    """Сообщения чатов с чеками, которых Claude ещё не видел (или их поправили)."""
    out = []
    for ch in chats(conn):
        seen = {r["msg_id"]: r["hash"] for r in conn.execute(
            "SELECT msg_id, hash FROM fin_seen WHERE chat_id=?", (ch["chat_id"],))}
        for r in conn.execute("SELECT * FROM chat_messages WHERE chat_id=? ORDER BY at, msg_id",
                              (ch["chat_id"],)):
            if seen.get(r["msg_id"]) != _hash(r):
                out.append(r)
    return out


def _local(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).astimezone(MSK).strftime("%d.%m %H:%M")
    except ValueError:
        return iso[:16]


def _line(r, short: int = 700) -> str:
    text = re.sub(r"\s*\n\s*", " / ", (r["text"] or "").strip())
    if len(text) > short:
        text = text[:short - 1] + "…"
    who = r["sender"] or "?"
    if r["username"]:
        who += f" (@{r['username']})"
    if r["outgoing"]:
        who = "Я (аккаунт сообщества, Марья)"
    reply = f" ↩{r['reply_to']}" if r["reply_to"] else ""
    media = f" [{r['media']}]" if r["media"] else ""
    return f"[{r['msg_id']}] {_local(r['at'])} {who}{reply}{media}: {text}"


def _examples(conn) -> str:
    rows = conn.execute("SELECT * FROM fin_ops WHERE edited_by IS NOT NULL ORDER BY edited_at DESC LIMIT 30").fetchall()
    if not rows:
        return "(пока нет)"
    return "\n".join(f"- «{(r['purpose'] or '')[:80]}» ({r['author'] or '?'}, {r['kind']} {r['amount']:.0f}) → "
                     f"проект {r['project'] or '—'}, подпроект {r['subproject'] or '—'}, статья {r['article'] or '—'}"
                     + (f", кошелёк {r['wallet']}" if r["wallet"] else "") for r in rows)


def _open_ops(conn) -> str:
    since = (datetime.now() - timedelta(days=45)).isoformat()
    rows = conn.execute("SELECT * FROM fin_ops WHERE status='заявка' OR (created_at>? AND status<>'отменено') "
                        "ORDER BY at DESC LIMIT 70", (since,)).fetchall()
    if not rows:
        return "(пока нет)"
    return "\n".join(f"op {r['id']}: сообщение [{r['msg_id']}] {_local(r['at'])} {r['author'] or ''} — {r['kind']} "
                     f"{r['amount']:.0f} ₽, {r['project'] or '—'} / {r['article'] or '—'}, {r['purpose'] or ''}"
                     f" [{r['status']}]" for r in rows)


def build_prompt(conn, batch: list[sqlite3.Row]) -> str:
    d = dictionary(conn)
    ids = {r["msg_id"] for r in batch}
    lines = []
    for r in batch:
        lines.append(_line(r))
        # на что отвечают — если этого нет в пачке, показываем кратко
        if r["reply_to"] and r["reply_to"] not in ids:
            q = conn.execute("SELECT * FROM chat_messages WHERE chat_id=? AND msg_id=?",
                             (r["chat_id"], r["reply_to"])).fetchone()
            if q and (q["text"] or "").strip():
                lines.append(f"    (это ответ на {_line(q, 200)})")
    return PROMPT.format(
        dictionary="\n".join(f"- {k}: " + "; ".join(v) for k, v in d.items()),
        examples=_examples(conn), open_ops=_open_ops(conn), messages="\n".join(lines))


def parse(raw: str) -> dict:
    raw = (raw or "").strip()
    m = re.search(r"\{.*\}", raw, flags=re.S)
    if not m:
        raise ValueError("Claude ответил не JSON")
    data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("Claude ответил не по форме")
    return data


def _num(v) -> float | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if isinstance(v, str):
        s = re.sub(r"[^\d,.\-]", "", v).replace(",", ".")
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _pick(value, allowed: list[str]) -> str | None:
    """Значение из справочника: точное, без учёта регистра или null."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    for a in allowed:
        if a.lower() == v.lower():
            return a
    return v[:80]         # не из справочника — оставим как есть, а человек увидит и поправит


def apply(conn, batch: list[sqlite3.Row], data: dict) -> int:
    """Записать разбор пачки. Операции, поправленные человеком, не трогаем."""
    d = dictionary(conn)
    by_id = {r["msg_id"]: r for r in batch}
    chat_of = {r["msg_id"]: r["chat_id"] for r in batch}
    n = 0
    got: dict[int, set[int]] = {}
    for o in data.get("ops") or []:
        if not isinstance(o, dict):
            continue
        msg = o.get("msg")
        amount = _num(o.get("amount"))
        if msg not in by_id or not amount or amount <= 0:
            continue
        r = by_id[msg]
        part = int(o.get("part") or 0)
        cur = conn.execute("SELECT * FROM fin_ops WHERE chat_id=? AND msg_id=? AND part=?",
                           (r["chat_id"], msg, part)).fetchone()
        got.setdefault(msg, set()).add(part)
        if cur and cur["edited_by"]:
            continue
        kind = o.get("kind") if o.get("kind") in KINDS else "расход"
        status = o.get("status") if o.get("status") in STATUSES else ("поступило" if kind == "приход" else "заявка")
        if kind == "приход" and status == "заявка":
            status = "поступило"
        wallet = _pick(o.get("wallet"), d["кошелёк"])
        # «оплачено» без кошелька — догадка Claude; оплату ставят платёжка или payments ниже
        if kind == "расход" and status == "оплачено" and not wallet:
            status = "заявка"
        art = _pick(o.get("article"), d["статья прихода"] if kind == "приход" else d["статья расхода"])
        opts = o.get("options") if isinstance(o.get("options"), dict) else {}
        opts = {k: [str(x)[:80] for x in v][:4] for k, v in opts.items()
                if k in ("project", "subproject", "article", "wallet", "kind") and isinstance(v, list) and v}
        q = (o.get("question") or None)
        conf = _num(o.get("confidence"))
        if not q and conf is not None and conf < 0.7:
            q = "Claude не уверен в разноске — проверьте проект и статью."
        vals = dict(
            at=r["at"], author=r["sender"], kind=kind, amount=round(amount, 2),
            project=_pick(o.get("project"), d["проект"]), subproject=_pick(o.get("subproject"), d["подпроект"]),
            article=art, counterparty=(o.get("counterparty") or None), purpose=(o.get("purpose") or None),
            wallet=wallet, participant=(o.get("participant") or None),
            engaged_by=(o.get("engaged_by") or None), status=status,
            question=str(q)[:300] if q else None, options=json.dumps(opts, ensure_ascii=False) if opts else None,
            confidence=conf)
        if cur:
            # оплату, которую уже отметили, разбор не откатывает
            if cur["status"] == "оплачено" and vals["status"] == "заявка":
                vals["status"] = "оплачено"
                vals["wallet"] = vals["wallet"] or cur["wallet"]
            conn.execute("UPDATE fin_ops SET " + ", ".join(f"{k}=?" for k in vals) + " WHERE id=?",
                         (*vals.values(), cur["id"]))
        else:
            conn.execute("INSERT INTO fin_ops (chat_id, msg_id, part, " + ", ".join(vals) + ") VALUES (?,?,?,"
                         + ",".join("?" * len(vals)) + ")", (r["chat_id"], msg, part, *vals.values()))
        n += 1
    # сообщение поправили и операций в нём стало меньше — лишние (не тронутые людьми) убираем
    for msg, r in by_id.items():
        if conn.execute("SELECT 1 FROM fin_seen WHERE chat_id=? AND msg_id=?", (r["chat_id"], msg)).fetchone():
            keep = got.get(msg, set())
            for x in conn.execute("SELECT id, part FROM fin_ops WHERE chat_id=? AND msg_id=? AND edited_by IS NULL "
                                  "AND paid_msg_id IS NULL", (r["chat_id"], msg)).fetchall():
                if x["part"] not in keep:
                    conn.execute("DELETE FROM fin_ops WHERE id=?", (x["id"],))
    for p in data.get("payments") or []:
        if not isinstance(p, dict) or p.get("msg") not in by_id:
            continue
        r = by_id[p["msg"]]
        _mark_paid(conn, r["chat_id"], p.get("request_msg"), r, p.get("part"),
                   _pick(p.get("wallet"), d["кошелёк"]), p.get("note"))
    for u in data.get("updates") or []:
        if not isinstance(u, dict) or not isinstance(u.get("fields"), dict):
            continue
        op = conn.execute("SELECT * FROM fin_ops WHERE id=?", (u.get("op"),)).fetchone()
        if not op or op["edited_by"]:
            continue
        f = {k: v for k, v in u["fields"].items() if k in FIELDS and k != "at"}
        if "amount" in f:
            f["amount"] = _num(f["amount"]) or op["amount"]
        if "status" in f and f["status"] not in STATUSES:
            f.pop("status")
        if f:
            note = ((op["note"] or "") + f"\nClaude: {u.get('why') or 'уточнено'}").strip()[:600]
            conn.execute("UPDATE fin_ops SET " + ", ".join(f"{k}=?" for k in f) + ", note=? WHERE id=?",
                         (*f.values(), note, op["id"]))
    for c in data.get("cancel") or []:
        if not isinstance(c, dict):
            continue
        op = conn.execute("SELECT * FROM fin_ops WHERE id=?", (c.get("op"),)).fetchone()
        if op and not op["edited_by"]:
            conn.execute("UPDATE fin_ops SET status='отменено', note=? WHERE id=?",
                         (((op["note"] or "") + f"\nОтменено: {c.get('why') or ''}").strip()[:600], op["id"]))
    for r in batch:
        conn.execute("INSERT OR REPLACE INTO fin_seen VALUES (?,?,?,datetime('now'))",
                     (r["chat_id"], r["msg_id"], _hash(r)))
    _payer_rule(conn, [r["chat_id"] for r in batch][:1])
    return n


def _mark_paid(conn, chat_id: int, request_msg, by: sqlite3.Row, part=None, wallet=None, note=None) -> None:
    if not request_msg:
        return
    q = "SELECT * FROM fin_ops WHERE chat_id=? AND msg_id=? AND status='заявка'"
    args: list[Any] = [chat_id, request_msg]
    if part is not None:
        q += " AND part=?"
        args.append(int(part))
    for op in conn.execute(q, args).fetchall():
        conn.execute("UPDATE fin_ops SET status='оплачено', paid_msg_id=?, paid_at=?, paid_note=?, "
                     "wallet=COALESCE(wallet, ?) WHERE id=?",
                     (by["msg_id"], by["at"], (note or f"оплатил(а) {by['sender'] or ''}")[:200], wallet, op["id"]))


def _payer_rule(conn, chat_ids: list[int]) -> None:
    """Подстраховка без Claude: платёжка Татьяны в ответ на заявку = оплачено."""
    for chat_id in chat_ids:
        for r in conn.execute("SELECT * FROM chat_messages WHERE chat_id=? AND username=? AND media IS NOT NULL "
                              "AND reply_to IS NOT NULL", (chat_id, PAYER)).fetchall():
            _mark_paid(conn, chat_id, r["reply_to"], r, note="платёжка Татьяны")


def _ask(prompt: str) -> str:
    done = subprocess.run(["claude", "-p"], input=prompt, capture_output=True, text=True,
                          timeout=1200, env=claude_env.env())
    out = (done.stdout or "").strip()
    if done.returncode != 0 or not out:
        raise RuntimeError(claude_env.explain((done.stderr or out or "пустой ответ").strip()[:300]))
    return out


def last_run(conn) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM fin_runs ORDER BY id DESC LIMIT 1").fetchone()


_lock = threading.Lock()


def is_running(conn) -> bool:
    return _lock.locked() or bool(conn.execute(
        "SELECT 1 FROM fin_runs WHERE status='идёт' AND started > datetime('now', '-30 minutes')").fetchone())


def run(conn, fetch: bool = True, ask=None, slot: str | None = None, limit: int | None = None) -> str:
    """Дочитать чат и разобрать новое. Возвращает итог строкой."""
    if not _lock.acquire(blocking=False):
        return "Уже разбирается."
    # между процессами (расписание, ручной запуск из консоли) — замок в базе
    conn.execute("BEGIN IMMEDIATE")
    busy = conn.execute("SELECT 1 FROM fin_runs WHERE status='идёт' AND started > datetime('now', '-30 minutes')").fetchone()
    if busy:
        conn.execute("ROLLBACK")
        _lock.release()
        return "Уже разбирается."
    rid = conn.execute("INSERT INTO fin_runs (started, status, slot) VALUES (datetime('now'), 'идёт', ?)",
                       (slot,)).lastrowid
    conn.execute("COMMIT")
    msgs = ops = 0
    try:
        if fetch:
            from . import reports
            for ch in chats(conn):
                reports.fetch(conn, ch["chat_id"], days=3)
        todo = pending(conn)
        if limit:
            todo = todo[:limit]
        for i in range(0, len(todo), BATCH):
            batch = todo[i:i + BATCH]
            data = parse((ask or _ask)(build_prompt(conn, batch)))
            ops += apply(conn, batch, data)
            msgs += len(batch)
            conn.execute("UPDATE fin_runs SET msgs=?, ops=?, note=? WHERE id=?",
                         (msgs, ops, f"разобрано {msgs} из {len(todo)}", rid))
        note = f"новых сообщений: {msgs}, операций записано: {ops}" if msgs else "новых сообщений нет"
        conn.execute("UPDATE fin_runs SET status='готово', finished=datetime('now'), note=?, msgs=?, ops=? WHERE id=?",
                     (note, msgs, ops, rid))
        return note
    except Exception as exc:
        conn.execute("UPDATE fin_runs SET status='ошибка', finished=datetime('now'), note=?, msgs=?, ops=? WHERE id=?",
                     (str(exc)[:300], msgs, ops, rid))
        return f"Ошибка: {exc}"
    finally:
        _lock.release()


def start(conn) -> str:
    """Разобрать сейчас, в фоне (кнопка на странице)."""
    if is_running(conn):
        return "Уже разбирается."
    if not claude_env.token():
        return "Claude не подключён."

    def work():
        c = store.connect()
        try:
            run(c)
        finally:
            c.close()
    threading.Thread(target=work, daemon=True, name="finance-run").start()
    return ""


def start_background() -> None:
    """В 13:00 и 21:00 по Москве — дочитать чат и разобрать новое."""
    import time

    def loop():
        while True:
            time.sleep(300)
            now = datetime.now(MSK)
            if now.hour not in RUN_HOURS:
                continue
            slot = now.strftime("%Y-%m-%d ") + str(now.hour)
            c = store.connect()
            try:
                init(c)
                if chats(c) and claude_env.token() and not c.execute(
                        "SELECT 1 FROM fin_runs WHERE slot=? OR started > datetime('now', '-3 hours')",
                        (slot,)).fetchone():
                    run(c, slot=slot)
            except Exception as exc:       # noqa: BLE001 — фоновая задача не должна падать
                print("finance:", exc, flush=True)
            finally:
                c.close()
    threading.Thread(target=loop, daemon=True, name="finance").start()


# ----------------------------------------------------------------- правка руками

def edit(conn, op_id: int, fields: dict, who: str) -> None:
    op = conn.execute("SELECT * FROM fin_ops WHERE id=?", (op_id,)).fetchone()
    if not op:
        raise ValueError("Операции нет.")
    f = {}
    for k, v in fields.items():
        if k not in FIELDS:
            continue
        v = (v or "").strip() if isinstance(v, str) else v
        if k == "amount":
            v = _num(v)
            if not v or v <= 0:
                raise ValueError("Сумма должна быть больше нуля.")
        elif k == "kind" and v not in KINDS:
            raise ValueError("Неверный вид операции.")
        elif k == "status" and v not in STATUSES:
            raise ValueError("Неверный статус.")
        f[k] = v or None if k not in ("amount", "kind", "status") else v
    if f.get("status") == "оплачено" and op["status"] != "оплачено" and not op["paid_at"]:
        f["paid_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        f["paid_note"] = f"отметил(а) {who}"
    f.update(edited_by=who, edited_at=datetime.now().isoformat(timespec="seconds"), question=None, options=None)
    conn.execute("UPDATE fin_ops SET " + ", ".join(f"{k}=?" for k in f) + " WHERE id=?", (*f.values(), op_id))


def pick(conn, op_id: int, field: str, value: str, who: str) -> None:
    """Ответ на вопрос Claude одним нажатием."""
    if field not in ("project", "subproject", "article", "wallet", "kind"):
        raise ValueError("Это поле так не выбирается.")
    edit(conn, op_id, {field: value}, who)


def add(conn, fields: dict, who: str) -> int:
    oid = conn.execute("INSERT INTO fin_ops (at, author, source, status) VALUES (?, ?, 'вручную', 'оплачено')",
                       (datetime.now(timezone.utc).isoformat(timespec="seconds"), who)).lastrowid
    try:
        edit(conn, oid, fields, who)
    except ValueError:
        conn.execute("DELETE FROM fin_ops WHERE id=?", (oid,))
        raise
    return oid


def set_wallet(conn, name: str, amount, start_date: str | None) -> None:
    v = _num(amount)
    conn.execute("INSERT OR REPLACE INTO fin_wallets VALUES (?,?,?)", (name, v or 0, start_date or None))


# ----------------------------------------------------------------- отчёты

def _day(op) -> str:
    """Дата движения денег: оплаты — когда оплатили, остальное — когда написали."""
    iso = op["paid_at"] if op["status"] == "оплачено" and op["paid_at"] else op["at"]
    try:
        return datetime.fromisoformat(iso).astimezone(MSK).date().isoformat() if iso else ""
    except ValueError:
        return (iso or "")[:10]


def ops(conn, **flt) -> list[dict]:
    q, args = "SELECT * FROM fin_ops WHERE 1=1", []
    if flt.get("id"):
        q += " AND id=?"
        args.append(int(flt["id"]))
    for k in ("kind", "project", "article", "status", "wallet"):
        if flt.get(k):
            if flt[k] == "—":
                q += f" AND ({k} IS NULL OR {k}='')"
            else:
                q += f" AND {k}=?"
                args.append(flt[k])
    if flt.get("q"):
        q += " AND (purpose LIKE ? OR counterparty LIKE ? OR participant LIKE ? OR author LIKE ?)"
        args += [f"%{flt['q']}%"] * 4
    out = []
    for r in conn.execute(q + " ORDER BY COALESCE(at, created_at) DESC, part", args).fetchall():
        d = dict(r)
        d["day"] = _day(r)
        d["link"] = msg_link(r["chat_id"], r["msg_id"]) if r["chat_id"] else None
        d["paid_link"] = msg_link(r["chat_id"], r["paid_msg_id"]) if r["chat_id"] else None
        d["opts"] = json.loads(r["options"]) if r["options"] else {}
        out.append(d)
    month = flt.get("month")
    if month and month != "all":
        out = [o for o in out if o["day"].startswith(month)]
    return out


def months(conn) -> list[str]:
    ms = sorted({_day(r)[:7] for r in conn.execute("SELECT at, paid_at, status FROM fin_ops") if _day(r)})
    return ms


def summary(conn, month: str | None = None) -> dict:
    all_ops = ops(conn)
    sel = [o for o in all_ops if not month or month == "all" or o["day"].startswith(month)]
    moved = [o for o in sel if o["status"] in MOVED]
    inc = sum(o["amount"] for o in moved if o["kind"] == "приход")
    out_ = sum(o["amount"] for o in moved if o["kind"] == "расход")
    waiting = [o for o in all_ops if o["status"] == "заявка" and o["kind"] == "расход"]
    ask = [o for o in all_ops if o["question"] and o["status"] != "отменено"]
    # ДДС: статьи × месяцы (по всем месяцам, чтобы видеть динамику)
    ms = months(conn)[-6:]
    dds: dict[str, dict[str, dict[str, float]]] = {"приход": {}, "расход": {}}
    for o in all_ops:
        if o["status"] not in MOVED or o["kind"] not in dds:
            continue
        row = dds[o["kind"]].setdefault(o["article"] or "Без статьи", {})
        row[o["day"][:7]] = row.get(o["day"][:7], 0) + o["amount"]
    totals = {k: {m: sum(r.get(m, 0) for r in v.values()) for m in ms} for k, v in dds.items()}
    # проекты за выбранный месяц
    proj: dict[str, dict[str, float]] = {}
    for o in sel:
        if o["status"] == "отменено" or o["kind"] == "перевод":
            continue
        p = proj.setdefault(o["project"] or "Без проекта", {"in": 0, "out": 0, "wait": 0})
        if o["status"] in MOVED:
            p["in" if o["kind"] == "приход" else "out"] += o["amount"]
        elif o["status"] == "заявка":
            p["wait"] += o["amount"]
    return {"inc": inc, "out": out_, "net": inc - out_, "n": len(sel),
            "waiting": waiting, "waiting_sum": sum(o["amount"] for o in waiting), "ask": ask,
            "months": ms, "dds": {k: sorted(v.items(), key=lambda x: -sum(x[1].values())) for k, v in dds.items()},
            "dds_totals": totals,
            "projects": sorted(proj.items(), key=lambda x: -(x[1]["in"] + x[1]["out"] + x[1]["wait"]))}


def wallets(conn) -> list[dict]:
    names = dictionary(conn)["кошелёк"]
    start = {r["name"]: r for r in conn.execute("SELECT * FROM fin_wallets")}
    all_ops = [o for o in ops(conn) if o["status"] in MOVED]
    out = []
    for n in names:
        s = start.get(n)
        since = s["start_date"] if s and s["start_date"] else ""
        mine = [o for o in all_ops if o["wallet"] == n and o["day"] >= since]
        inc = sum(o["amount"] for o in mine if o["kind"] == "приход")
        exp = sum(o["amount"] for o in mine if o["kind"] == "расход")
        out.append({"name": n, "start": s["start_amount"] if s else 0, "since": since, "inc": inc, "out": exp,
                    "left": (s["start_amount"] if s else 0) + inc - exp, "n": len(mine)})
    nowallet = [o for o in all_ops if not o["wallet"] and o["kind"] != "перевод"]
    return out, len(nowallet)


def cards_by_name(conn) -> dict[frozenset, int]:
    """Участники по имени — чтобы поступление вело в карточку того, кто оплатил."""
    out = {}
    for r in conn.execute("SELECT id, name FROM people"):
        key = frozenset((r["name"] or "").lower().replace("ё", "е").split())
        if len(key) >= 2:
            out.setdefault(key, -r["id"])
    return out


def card_of(index: dict, name: str | None) -> int | None:
    if not name:
        return None
    return index.get(frozenset(name.lower().replace("ё", "е").split()))
