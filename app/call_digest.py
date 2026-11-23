"""Созвон по участникам — один запрос на всю встречу.

На созвоне команды шесть-восемь человек. Разбирать каждого отдельным запросом
значит отправлять всю расшифровку шесть-восемь раз. Здесь Claude получает
расшифровку один раз вместе с короткими справками всех узнанных участников
(context.person: анкета, сведения, цели и шаги, прошлый разбор) и отвечает по
каждому: что рассказал, что сделал по шагам, что пообещал, тревожный сигнал,
новые сведения о себе. Заметки ложатся в карточки участников, сведения —
предложениями в «Кто это» (принимает организатор).
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import threading
from datetime import datetime

from . import calls, claude_env, context, person_info, store

LIMIT = 200_000      # знаков расшифровки в запрос: полуторачасовая встреча ~90 тыс.

SCHEMA = """
CREATE TABLE IF NOT EXISTS call_notes (
    call_id    TEXT NOT NULL,
    card       INTEGER NOT NULL,
    call_title TEXT,
    call_when  TEXT,
    note       TEXT,
    promises   TEXT,                 -- JSON: что пообещал
    alarm      TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (call_id, card)
);
CREATE INDEX IF NOT EXISTS idx_call_notes_card ON call_notes(card, created_at);
CREATE TABLE IF NOT EXISTS call_team_notes (
    call_id    TEXT NOT NULL,
    flow       TEXT NOT NULL,
    team_title TEXT NOT NULL,        -- teams.title, как у people.team_id
    call_title TEXT, call_when TEXT,
    note       TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (call_id, flow, team_title)
);
CREATE TABLE IF NOT EXISTS call_digest_runs (
    call_id TEXT PRIMARY KEY,
    status  TEXT NOT NULL,           -- идёт | готово | ошибка
    note    TEXT, at TEXT
);
"""

PROMPT = """Ты помогаешь организаторам и менторам бизнес-сообщества «Вершина» (мастермайнды
для предпринимателей). Ниже расшифровка созвона «{title}» ({when}). Расшифровка
автоматическая: возможны ошибки в словах. Говорящие подписаны именами.

Участники на встрече — код, имя, команда и что о нём уже известно (анкета, карточка, цели
и шаги декларации, прошлый разбор, что обещал на прошлом созвоне):
{people}

По КАЖДОМУ участнику из списка напиши, что важно его ментору и ему самому, и по каждой
команде — общий абзац. Ответ — только JSON без пояснений и markdown:

{{"people": {{"P1": {{
    "spoke": true,
    "state": "идёт" | "буксует" | "выпадает",
    "note": "2–5 предложений ментору: что рассказал о делах и прогрессе; какие шаги из его
             декларации сделал (называй их); где буксует; что сказали ему ментор и группа",
    "approach": "как ментору сейчас с ним говорить: какой подход сработает (поддержать,
                 надавить, разобрать страх, похвалить за конкретное) и 1–2 фразы, которые
                 стоит попробовать сказать, — из того, на что он откликается",
    "for_member": "2–3 предложения самому участнику, на «ты», бережно: что у него сейчас
                   происходит, что он пообещал, на чём сфокусироваться до следующей встречи.
                   Без чужих оценок, резкостей и ярлыков",
    "promises": ["что пообещал к следующему разу — со сроком, если звучал"],
    "promises_check": [{{"promise": "обещание с прошлого созвона из справки", "done": true | false | null}}],
    "alarm": "тревожный сигнал одной фразой (выпадает, не делает, конфликт) или null",
    "facts": {{"city": факт, "work": факт, "income": факт, "request": факт, "about": факт}}}}}},
 "teams": {{"название команды, как в справке": "2–4 предложения ментору: атмосфера в команде,
            кто тянет, кто выпадает, на что обратить внимание до следующей встречи"}}}}

Правила:
— только то, что прозвучало на встрече; сверяй со справкой, расхождения отмечай;
— участник молчал или почти не говорил — "spoke": false, "note" и "for_member": null,
  "state" — по справке, если по ней видно, иначе null;
— «promises_check» — только обещания из справки; «done»: сказал, что сделал, — true,
  сказал, что не сделал, или по разговору видно — false, не звучало — null;
— «facts» — каждое {{"value": "коротко", "quote": "дословная фраза"}} или null; только
  сказанное самим участником о себе, только устойчивое (где живёт, чем занимается, доход,
  семья — не «сегодня работал из дома») и только новое или отличное от справки;
— никаких диагнозов и ярлыков.

Расшифровка:
{transcript}
"""


STATES = ("идёт", "буксует", "выпадает")


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(call_notes)")}
    for col in ("state", "approach", "member_note", "promises_check"):
        if col not in have:
            conn.execute(f"ALTER TABLE call_notes ADD COLUMN {col} TEXT")


def _stamp(sec) -> str:
    sec = int(sec or 0)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60:02d}:{sec % 60:02d}"


def build_prompt(conn: sqlite3.Connection, call: dict) -> tuple[str, dict[str, int]]:
    """Запрос и коды участников → карточки. Нет узнанных участников — ("", {})."""
    members = [p for p in call["people"] if p["kind"] == "участник" and p["ref"]]
    codes, blocks, seen = {}, [], set()
    for p in members:
        if p["ref"] in seen:
            continue
        seen.add(p["ref"])
        code = f"P{len(codes) + 1}"
        codes[code] = p["ref"]
        about = context.person(conn, p["ref"], skip_call=call["id"]) or p["name"]
        blocks.append(f"[{code}] {p['name']} (в записи «{p['label']}»)\n{about}")
    if not codes:
        return "", {}
    names = {p["label"]: (p["name"] or p["label"]) for p in call["people"]}
    staff = {p["label"] for p in call["people"] if p["kind"] == "команда"}
    lines, last = [], None
    for t in call["turns"]:
        who = names.get(t.get("who"), t.get("who") or "—")
        if t.get("who") in staff:
            who += " (ментор/организатор)"
        text = (t.get("text") or "").strip()
        if not text:
            continue
        if who == last:
            lines[-1] += " " + text
        else:
            lines.append(f"[{_stamp(t.get('start'))}] {who}: {text}")
            last = who
    transcript = "\n".join(lines)[:LIMIT]
    when = call["when"].strftime("%d.%m.%Y") if call.get("when") else "дата неизвестна"
    return PROMPT.format(title=calls.title_of(call), when=when, people="\n\n".join(blocks),
                         transcript=transcript), codes


def parse(raw: str, codes: dict[str, int]) -> tuple[dict[int, dict], dict[str, str]]:
    """Ответ → ({карточка: заметка}, {команда: абзац})."""
    m = re.search(r"\{.*\}", raw or "", flags=re.S)
    if not m:
        return {}, {}
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return {}, {}
    people = d.get("people") if isinstance(d.get("people"), dict) else d   # старая форма — без обёртки
    teams = d.get("teams") if isinstance(d.get("teams"), dict) else {}
    return ({codes[k]: v for k, v in people.items() if k in codes and isinstance(v, dict)},
            {str(k): str(v) for k, v in teams.items() if isinstance(v, str) and v.strip()})


def _team_titles(conn: sqlite3.Connection, cards: list[int]) -> dict[str, tuple[str, str]]:
    """Короткое имя команды → (поток, teams.title) — по участникам встречи."""
    out = {}
    for card in cards:
        r = conn.execute("SELECT p.flow, t.title FROM people p JOIN teams t ON t.id=p.team_id WHERE p.id=?",
                         (-card,)).fetchone()
        if r:
            out[r["title"].split("—")[0].strip().lower()] = (r["flow"], r["title"])
            out[r["title"].strip().lower()] = (r["flow"], r["title"])
    return out


def save(conn: sqlite3.Connection, call: dict, res: dict[int, dict], teams: dict[str, str] | None = None) -> int:
    title = calls.title_of(call)
    when = call["when"].isoformat(timespec="minutes") if call.get("when") else None
    src = f"созвон {call['when'].strftime('%d.%m') if call.get('when') else ''} — {title}".replace("  ", " ")
    now = datetime.now().isoformat(timespec="seconds")
    n = 0
    for card, v in res.items():
        spoke = v.get("spoke", True)
        note = (v.get("note") or "").strip() if spoke else ""
        promises = [str(x).strip() for x in (v.get("promises") or []) if str(x).strip()]
        check = [c for c in (v.get("promises_check") or []) if isinstance(c, dict) and c.get("promise")]
        state = v.get("state") if v.get("state") in STATES else None
        conn.execute("INSERT OR REPLACE INTO call_notes (call_id, card, call_title, call_when, note, promises, alarm, "
                     "state, approach, member_note, promises_check, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (call["id"], card, title, when, note or None, json.dumps(promises, ensure_ascii=False),
                      (v.get("alarm") or None) if isinstance(v.get("alarm"), str) else None,
                      state, _s(v.get("approach")), _s(v.get("for_member")) if spoke else None,
                      json.dumps(check, ensure_ascii=False), now))
        person_info.propose(conn, card, v.get("facts"), src, call["id"])
        n += 1
    known = _team_titles(conn, list(res))
    conn.execute("DELETE FROM call_team_notes WHERE call_id=?", (call["id"],))
    for name, text in (teams or {}).items():
        hit = known.get(name.strip().lower()) or known.get(name.split("—")[0].strip().lower())
        if hit:
            conn.execute("INSERT OR REPLACE INTO call_team_notes (call_id, flow, team_title, call_title, call_when, note, created_at) "
                         "VALUES (?,?,?,?,?,?,?)", (call["id"], hit[0], hit[1], title, when, text.strip(), now))
    return n


def _s(v) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() and v.strip().lower() != "null" else None


def last_note(conn: sqlite3.Connection, card: int):
    """Свежая заметка со созвона по участнику (по времени встречи)."""
    return conn.execute("SELECT * FROM call_notes WHERE card=? ORDER BY COALESCE(call_when, created_at) DESC LIMIT 1",
                        (card,)).fetchone()


def last_team_note(conn: sqlite3.Connection, flow: str, team_title: str):
    # две встречи в один час (команды разбирали в разных комнатах) — берём ту, где команды было больше
    return conn.execute(
        "SELECT n.*, (SELECT COUNT(*) FROM call_notes c JOIN people p ON p.id=-c.card JOIN teams t ON t.id=p.team_id "
        "             WHERE c.call_id=n.call_id AND t.title=n.team_title) AS here "
        "FROM call_team_notes n WHERE n.flow=? AND n.team_title=? "
        "ORDER BY substr(COALESCE(n.call_when, n.created_at), 1, 13) DESC, here DESC LIMIT 1",
        (flow, team_title)).fetchone()


def of_call(conn: sqlite3.Connection, call_id: str) -> dict[int, sqlite3.Row]:
    return {r["card"]: r for r in conn.execute("SELECT * FROM call_notes WHERE call_id=?", (call_id,))}


def of_person(conn: sqlite3.Connection, card: int) -> dict[str, sqlite3.Row]:
    return {r["call_id"]: r for r in conn.execute("SELECT * FROM call_notes WHERE card=?", (card,))}


def run_state(conn: sqlite3.Connection, call_id: str):
    return conn.execute("SELECT * FROM call_digest_runs WHERE call_id=?", (call_id,)).fetchone()


def can_run(call: dict) -> bool:
    return any(p["kind"] == "участник" and p["ref"] for p in call["people"]) and bool(call["turns"])


def start(conn: sqlite3.Connection, call_id: str, ask=None) -> str:
    """Запустить разбор встречи в фоне. Возвращает пустую строку или причину отказа."""
    run = run_state(conn, call_id)
    if run and run["status"] == "идёт":
        return "Уже разбирается."
    if ask is None and not claude_env.token():
        return "Claude не подключён."
    call = calls.get(conn, call_id)
    if not call or not can_run(call):
        return "На встрече нет узнанных участников — сначала отметьте, кто есть кто."
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("INSERT OR REPLACE INTO call_digest_runs (call_id, status, note, at) VALUES (?,?,?,?)",
                 (call_id, "идёт", None, now))

    def work():
        own = store.connect()
        try:
            c = calls.get(own, call_id)
            prompt, codes = build_prompt(own, c)
            if ask:
                raw = ask(prompt)
            else:
                done = subprocess.run(["claude", "-p"], input=prompt, capture_output=True, text=True,
                                      timeout=1200, env=claude_env.env())
                raw = (done.stdout or "").strip()
                if done.returncode != 0 or not raw:
                    raise RuntimeError(claude_env.explain((done.stderr or raw or "пустой ответ").strip()[:300]))
            res, teams = parse(raw, codes)
            if not res:
                raise RuntimeError("Claude ответил не по форме")
            n = save(own, c, res, teams)
            own.execute("UPDATE call_digest_runs SET status='готово', note=?, at=? WHERE call_id=?",
                        (f"участников: {n}", datetime.now().isoformat(timespec="seconds"), call_id))
        except Exception as exc:
            own.execute("UPDATE call_digest_runs SET status='ошибка', note=?, at=? WHERE call_id=?",
                        (str(exc)[:300], datetime.now().isoformat(timespec="seconds"), call_id))
        finally:
            own.close()

    if ask:
        work()
    else:
        threading.Thread(target=work, daemon=True).start()
    return ""
