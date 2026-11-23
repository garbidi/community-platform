"""«Кто это?» — Claude угадывает неузнанных говорящих по разговору.

В Зуме люди заходят как «iPhone», «Пользователь Zoom», а безымянных
Стенограф подписывает «Голос 3». Но в разговоре их почти всегда называют:
ведущий передаёт слово по имени, к человеку обращаются, он рассказывает
о своём — а его цели есть в декларации. Claude читает реплики неузнанного
с соседними, сверяет со списком кандидатов потока и предлагает, кто это,
с цитатами-доказательствами. Ничего не записывается само: организатор
подтверждает, и только тогда подпись связывается с человеком.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import threading
from datetime import datetime

from . import calls, claude_env, store

MAX_TURNS = 30        # реплик одного неузнанного в промпт
CONFIDENCE = ("высокая", "средняя", "низкая")

PROMPT = """Ты помогаешь организаторам мастермайнда «Вершина» понять, кто говорит на записи созвона.
Часть говорящих подписана в Зуме непонятно («iPhone», «Пользователь Zoom», «Голос 3») или только
именем, которое носят несколько человек. Определи, кто это, по самому разговору:
- ведущий передаёт слово по имени прямо перед репликой («Глеб.», «Азат, давай ты»);
- к человеку обращаются по имени в ответ на его реплику;
- человек сам называет себя или свою команду;
- человек рассказывает о своём, и это совпадает с целями из его декларации.

Кандидаты — только из списка ниже (код в квадратных скобках). Уже узнанные на этой встрече люди
обычно не могут быть кем-то ещё — разве что перезашли с другого устройства.

Строго:
- выбирай кандидата, только если есть прямое доказательство в репликах; нет — "кандидат": null;
- "доказательства": 1–3 дословные цитаты из реплик ниже, с временем в квадратных скобках;
- "уверенность": "высокая" — назван по имени прямо перед или после своей реплики, без вариантов;
  "средняя" — несколько косвенных признаков; "низкая" — один косвенный признак;
- ответь только JSON, без пояснений вокруг:
{{"кто": [{{"подпись": "…", "кандидат": "P12" или null, "уверенность": "…", "доказательства": ["[07:12] …"], "почему": "одной фразой"}}]}}

Кандидаты:
{candidates}

Уже узнаны на встрече: {known}

Неузнанные и их реплики (с соседними репликами — там часто звучит имя):
{unknown}
"""


def _stamp(sec: float) -> str:
    sec = int(sec or 0)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60:02d}:{sec % 60:02d}"


def _line(t: dict, cut: int) -> str:
    text = (t.get("text") or "").strip()
    return f"[{_stamp(t.get('start'))}] {t.get('who') or '—'}: {text[:cut]}{'…' if len(text) > cut else ''}"


def targets(call: dict) -> list[str]:
    """Кого разбирать: не узнан, или узнан лишь «похоже» по одному имени."""
    return [p["label"] for p in call["people"]
            if p["how"] in ("—", "догадка") and p["kind"] != "чужой"]


def _candidates(conn: sqlite3.Connection, call: dict) -> tuple[list[dict], str]:
    """Участники потока встречи (а без потока — всех идущих) и команда."""
    from .roster import card_id
    flow = call["flow"]
    rows = conn.execute(
        "SELECT p.id, p.name, p.flow, t.title AS team FROM people p LEFT JOIN teams t ON t.id=p.team_id "
        "WHERE p.status='в потоке'" + (" AND p.flow=?" if flow else ""), ((flow,) if flow else ())).fetchall()
    out, lines = [], []
    for r in rows:
        goals = [g[0] for g in conn.execute(
            "SELECT title FROM decl_goals WHERE person_id=? ORDER BY no LIMIT 6", (r["id"],))]
        code = f"P{r['id']}"
        out.append({"code": code, "kind": "участник", "ref": card_id(r["id"]), "name": r["name"]})
        team = (r["team"] or "").split("—")[0].strip()
        goal_txt = "; ".join(g[:80] for g in goals)
        lines.append(f"[{code}] {r['name']} — участник {r['flow']}{', ' + team if team else ''}"
                     + (f". Цели: {goal_txt}" if goal_txt else ""))
    for u in conn.execute("SELECT id, name, role FROM users WHERE active=1 AND role IN "
                          "('mentor','coordinator','admin') ORDER BY name"):
        code = f"U{u['id']}"
        role = {"mentor": "ментор", "coordinator": "координатор", "admin": "организатор"}[u["role"]]
        out.append({"code": code, "kind": "команда", "ref": u["id"], "name": u["name"]})
        lines.append(f"[{code}] {u['name']} — {role}")
    return out, "\n".join(lines)


def build_prompt(conn: sqlite3.Connection, call: dict, labels: list[str]) -> tuple[str, list[dict]]:
    cands, cand_txt = _candidates(conn, call)
    known = ", ".join(f"{p['name']} (в Зуме «{p['label']}»)" for p in call["people"]
                      if p["kind"] and p["how"] in ("узнан", "вручную")) or "никто"
    turns = call["turns"]
    blocks = []
    for label in labels:
        idx = [i for i, t in enumerate(turns) if t.get("who") == label]
        # самые длинные реплики плюс первые — там чаще представляются
        chosen = sorted(set(idx[:5]) | set(sorted(idx, key=lambda i: -len(turns[i].get("text") or ""))[:MAX_TURNS - 5]))
        parts = [f"### «{label}» — реплик всего {len(idx)}"]
        for i in chosen:
            if i > 0:
                parts.append("   до: " + _line(turns[i - 1], 220))
            parts.append(">> " + _line(turns[i], 400))
            if i + 1 < len(turns):
                parts.append("   после: " + _line(turns[i + 1], 220))
        blocks.append("\n".join(parts))
    return PROMPT.format(candidates=cand_txt, known=known, unknown="\n\n".join(blocks)), cands


def parse(raw: str, labels: list[str], cands: list[dict]) -> list[dict]:
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        raise ValueError("Claude ответил не JSON")
    data = json.loads(m.group(0))
    by_code = {c["code"]: c for c in cands}
    out = []
    for g in data.get("кто") or []:
        label = (g.get("подпись") or "").strip()
        if label not in labels:
            continue
        c = by_code.get((g.get("кандидат") or "").strip())
        conf = g.get("уверенность") if g.get("уверенность") in CONFIDENCE else "низкая"
        ev = [str(e)[:300] for e in (g.get("доказательства") or [])][:3]
        out.append({"label": label, "kind": c["kind"] if c else None, "ref": c["ref"] if c else None,
                    "confidence": conf if c else None, "evidence": ev,
                    "why": (g.get("почему") or "").strip()[:300]})
    return out


def save(conn: sqlite3.Connection, call_id: str, guesses: list[dict]) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    for g in guesses:
        old = conn.execute("SELECT status, kind, ref FROM call_guesses WHERE call_id=? AND label=?",
                           (call_id, g["label"])).fetchone()
        # уже принятое или отклонённое то же самое не переспрашиваем
        if old and old["status"] != "предложено" and (old["kind"], old["ref"]) == (g["kind"], g["ref"]):
            continue
        conn.execute("INSERT OR REPLACE INTO call_guesses VALUES (?,?,?,?,?,?,?,?,?)",
                     (call_id, g["label"], g["kind"], g["ref"], g["confidence"],
                      json.dumps(g["evidence"], ensure_ascii=False), g["why"], "предложено", now))


def of_call(conn: sqlite3.Connection, call_id: str) -> dict[str, dict]:
    out = {}
    for r in conn.execute("SELECT * FROM call_guesses WHERE call_id=?", (call_id,)):
        out[r["label"]] = {**dict(r), "evidence": json.loads(r["evidence"] or "[]")}
    return out


def run_state(conn: sqlite3.Connection, call_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM call_guess_runs WHERE call_id=?", (call_id,)).fetchone()


def start(conn: sqlite3.Connection, call_id: str, ask=None) -> str:
    """Запустить разбор в фоне. ask — подмена Claude для проверок."""
    call = calls.get(conn, call_id)
    if not call:
        return "нет встречи"
    labels = targets(call)
    if not labels:
        return "некого разбирать"
    st = run_state(conn, call_id)
    if st and st["status"] == "идёт":
        return "уже идёт"
    if ask is None and not claude_env.token():
        return "Claude не подключён"
    prompt, cands = build_prompt(conn, call, labels)
    conn.execute("INSERT OR REPLACE INTO call_guess_runs VALUES (?, 'идёт', NULL, ?)",
                 (call_id, datetime.now().isoformat(timespec="seconds")))

    def work():
        c = store.connect()
        try:
            try:
                if ask:
                    raw = ask(prompt)
                else:
                    done = subprocess.run(["claude", "-p"], input=prompt, capture_output=True, text=True,
                                          timeout=600, env=claude_env.env())
                    raw = (done.stdout or "").strip()
                    if done.returncode != 0 or not raw or "Not logged in" in raw:
                        raise RuntimeError(claude_env.explain((done.stderr or raw or "пустой ответ").strip()[:300]))
                got = parse(raw, labels, cands)
                save(c, call_id, got)
                n = sum(1 for g in got if g["kind"])
                c.execute("UPDATE call_guess_runs SET status='готово', note=?, at=? WHERE call_id=?",
                          (f"предложено: {n} из {len(labels)}", datetime.now().isoformat(timespec="seconds"), call_id))
            except Exception as exc:              # noqa: BLE001 — показываем на странице
                c.execute("UPDATE call_guess_runs SET status='ошибка', note=?, at=? WHERE call_id=?",
                          (str(exc)[:300], datetime.now().isoformat(timespec="seconds"), call_id))
        finally:
            c.close()
    t = threading.Thread(target=work, daemon=True)
    t.start()
    return "запущен" if ask is None else t


def decide(conn: sqlite3.Connection, call_id: str, label: str, accept: bool, who: str, everywhere: bool) -> None:
    g = conn.execute("SELECT * FROM call_guesses WHERE call_id=? AND label=?", (call_id, label)).fetchone()
    if not g or not g["kind"]:
        raise ValueError("нет подсказки")
    if accept:
        calls.set_alias(conn, label, g["kind"], g["ref"], who, "" if everywhere else call_id)
    conn.execute("UPDATE call_guesses SET status=? WHERE call_id=? AND label=?",
                 ("принято" if accept else "отклонено", call_id, label))
