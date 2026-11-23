"""Декларации на платформе — для потоков без Google-таблиц (с ММ8).

ММ7 ведётся в таблицах команд, и roster.py раз в час переносит их в базу:
people → decl_goals → decl_steps, involved, сводки в people. Здесь те же
таблицы заполняются прямо на платформе, поэтому все экраны, лидерборд и ИИ
работают одинаково для обоих видов потока. Участник такого потока помечен
people.source = 'платформа' — синхронизация с таблицами его не трогает.

Формулы — как на «Дашборде» таблиц ММ7 (сверено 15.10.2026):
- % цели = отмеченные шаги ÷ написанные (цель без шагов — 0, закрытая руками — 100);
- % участника = среднее по его целям; цель закрыта при 100%;
- планка по вовлечению и деньги в % декларации не входят — считаются отдельно;
- командная планка = сумма личных планок, включая выбывших: выбывший остаётся
  в команде, его вовлечённые — команде, его планка делится между оставшимися.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from . import flows

SCHEMA = """
-- Декларация на подпись: снимок целей, шагов и условий в момент печати.
-- Участник подписывает бумагу; после правок с трекером — новая версия и новая подпись.
CREATE TABLE IF NOT EXISTS decl_versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id   INTEGER NOT NULL,
    no          INTEGER NOT NULL,          -- версия 1, 2, 3…
    content     TEXT NOT NULL,             -- JSON-снимок (snapshot)
    hash        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'на подписи',   -- на подписи | подписана | заменена
    created_by  TEXT, created_at TEXT,
    signed_by   TEXT, signed_on TEXT,      -- кто отметил и дата подписи на бумаге
    UNIQUE (person_id, no)
);
CREATE TABLE IF NOT EXISTS money_weeks (
    person_id  INTEGER NOT NULL,
    week       INTEGER NOT NULL,          -- 1…N, неделя потока
    amount     INTEGER NOT NULL DEFAULT 0,
    note       TEXT,
    set_by     TEXT, set_at TEXT,
    PRIMARY KEY (person_id, week)
);
"""


class DeclError(ValueError):
    """Ошибка, которую можно показать человеку как есть."""


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(people)")}
    if "source" not in cols:
        conn.execute("ALTER TABLE people ADD COLUMN source TEXT NOT NULL DEFAULT 'таблица'")
    if "left_at" not in cols:
        conn.execute("ALTER TABLE people ADD COLUMN left_at TEXT")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(decl_steps)")}
    for col in ("done_by", "done_at"):
        if col not in cols:
            conn.execute(f"ALTER TABLE decl_steps ADD COLUMN {col} TEXT")
    if "closed" not in {r[1] for r in conn.execute("PRAGMA table_info(decl_goals)")}:
        # цель без шагов (или с особым подтверждением) закрывают руками
        conn.execute("ALTER TABLE decl_goals ADD COLUMN closed INTEGER NOT NULL DEFAULT 0")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _clean(text: str, what: str, limit: int = 300) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        raise DeclError(f"Напишите {what}.")
    if len(text) > limit:
        raise DeclError(f"Слишком длинно: {what} — до {limit} знаков.")
    return text


def _date(value: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    raise DeclError("Дата — в виде ДД.ММ.ГГГГ.")


# ----------------------------------------------------------------- кто где

def is_native_team(t) -> bool:
    """Команда ведётся на платформе, если у неё нет Google-таблицы."""
    return not (t["sheet_id"] or "").strip()


def person(conn, pid: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()


def editable(conn, pid: int) -> sqlite3.Row:
    p = person(conn, pid)
    if not p:
        raise DeclError("Участник не найден.")
    if p["source"] != "платформа":
        raise DeclError("Этот участник ведётся в Google-таблице команды — правьте там.")
    return p


def _legacy_team(conn, flow: str, title: str) -> int:
    """Группа в старой таблице teams — к ней привязаны участники (people.team_id)."""
    row = conn.execute("SELECT id FROM teams WHERE flow=? AND title=?", (flow, title)).fetchone()
    if row:
        return row["id"]
    return int(conn.execute("INSERT INTO teams (title, flow, goal, note) VALUES (?,?,0,?)",
                            (title, flow, "команда на платформе")).lastrowid)


def _flow_team(conn, flow_team_id: int) -> tuple[sqlite3.Row, sqlite3.Row]:
    t = conn.execute("SELECT * FROM flow_teams WHERE id=?", (flow_team_id,)).fetchone()
    if not t:
        raise DeclError("Команда не найдена.")
    if not is_native_team(t):
        raise DeclError("Эта команда ведётся в Google-таблице — участники берутся оттуда.")
    return t, flows.get(conn, t["flow_id"])


def _resync_staff(conn, flow_team_id: int) -> None:
    """Менторы и трекеры команды видят её участников — как при чтении таблиц."""
    from .roster import _set_team_members
    t, f = _flow_team(conn, flow_team_id)
    team_id = _legacy_team(conn, f["name"], t["title"])
    pids = [r["id"] for r in conn.execute(
        "SELECT id FROM people WHERE flow=? AND team_id=? AND status='в потоке'", (f["name"], team_id))]
    cfg = next(x for x in flows.config(conn, f)["teams"] if x["title"] == t["title"])
    _set_team_members(conn, team_id, pids, cfg)


# ----------------------------------------------------------------- участники

def add_person(conn, flow_team_id: int, name: str, username: str = "", planka: int = 0) -> int:
    """Новый участник команды на платформе. Имя — «Фамилия Имя», как в таблицах."""
    t, f = _flow_team(conn, flow_team_id)
    name = _clean(name, "фамилию и имя", 80)
    if conn.execute("SELECT 1 FROM people WHERE flow=? AND name=?", (f["name"], name)).fetchone():
        raise DeclError(f"«{name}» в потоке {f['name']} уже есть.")
    nick = (username or "").strip().lstrip("@") or None
    pid = int(conn.execute(
        "INSERT INTO people (flow, name, team_id, status, username, source, synced_at) "
        "VALUES (?,?,?, 'в потоке', ?, 'платформа', datetime('now'))",
        (f["name"], name, _legacy_team(conn, f["name"], t["title"]), nick)).lastrowid)
    set_planka(conn, pid, planka)
    _resync_staff(conn, flow_team_id)
    return pid


def add_loose(conn, flow_id: int, name: str, username: str = "", phone: str = "") -> int:
    """Участник потока без команд (4-дневный мастермайнд) — заведён руками, не из таблицы."""
    from .fourday import phone_key
    f = flows.get(conn, flow_id)
    if not f:
        raise DeclError("Поток не найден.")
    name = _clean(name, "фамилию и имя", 80)
    if conn.execute("SELECT 1 FROM people WHERE flow=? AND name=?", (f["name"], name)).fetchone():
        raise DeclError(f"«{name}» в потоке {f['name']} уже есть.")
    return int(conn.execute(
        "INSERT INTO people (flow, name, status, username, phone, source, synced_at) "
        "VALUES (?,?, 'в потоке', ?, ?, 'платформа', datetime('now'))",
        (f["name"], name, (username or "").strip().lstrip("@") or None, phone_key(phone))).lastrowid)


def set_left(conn, pid: int, left: bool) -> None:
    """Выбыл / вернулся. Его планка делится между оставшимися участниками команды."""
    p = editable(conn, pid)
    conn.execute("UPDATE people SET status=?, left_at=? WHERE id=?",
                 ("выбыл" if left else "в потоке", date.today().isoformat() if left else None, pid))
    t = _team_of(conn, p)
    if t:
        _resync_staff(conn, t["id"])
    recount_flow(conn, p["flow"])


def _team_of(conn, p) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT ft.* FROM flow_teams ft JOIN flows f ON f.id=ft.flow_id JOIN teams t ON t.title=ft.title "
        "AND t.flow=f.name WHERE t.id=? AND f.name=?", (p["team_id"], p["flow"])).fetchone()


def set_terms(conn, pid: int, word_price: str, perk: str, money_goal: str) -> None:
    """Цена слова, плюшка, цель в деньгах."""
    editable(conn, pid)
    money = re.sub(r"[^\d]", "", money_goal or "")
    conn.execute("UPDATE people SET word_price=?, perk=?, money_goal=? WHERE id=?",
                 ((word_price or "").strip() or None, (perk or "").strip() or None,
                  int(money) if money else None, pid))


# ----------------------------------------------------------------- цели и шаги

def add_goal(conn, pid: int, title: str, deadline: str = "", proof: str = "") -> int:
    editable(conn, pid)
    no = (conn.execute("SELECT MAX(no) FROM decl_goals WHERE person_id=?", (pid,)).fetchone()[0] or 0) + 1
    gid = int(conn.execute(
        "INSERT INTO decl_goals (person_id, no, title, deadline, progress, proof) VALUES (?,?,?,?,0,?)",
        (pid, no, _clean(title, "цель"), _date(deadline), (proof or "").strip() or None)).lastrowid)
    recount(conn, pid)
    return gid


def _goal(conn, gid: int) -> sqlite3.Row:
    g = conn.execute("SELECT * FROM decl_goals WHERE id=?", (gid,)).fetchone()
    if not g:
        raise DeclError("Цель не найдена.")
    editable(conn, g["person_id"])
    return g


def update_goal(conn, gid: int, title: str, deadline: str, proof: str) -> None:
    g = _goal(conn, gid)
    conn.execute("UPDATE decl_goals SET title=?, deadline=?, proof=? WHERE id=?",
                 (_clean(title, "цель"), _date(deadline), (proof or "").strip() or None, gid))
    recount(conn, g["person_id"])


def close_goal(conn, gid: int, closed: bool) -> None:
    """Закрыть цель руками — для целей без шагов (подарок, сумма, событие)."""
    g = _goal(conn, gid)
    conn.execute("UPDATE decl_goals SET closed=? WHERE id=?", (int(closed), gid))
    recount(conn, g["person_id"])


def remove_goal(conn, gid: int) -> None:
    g = _goal(conn, gid)
    conn.execute("DELETE FROM decl_steps WHERE goal_id=?", (gid,))
    conn.execute("DELETE FROM decl_goals WHERE id=?", (gid,))
    # номера целей подряд — как в декларации на подпись
    for n, r in enumerate(conn.execute("SELECT id FROM decl_goals WHERE person_id=? ORDER BY no",
                                       (g["person_id"],)).fetchall(), 1):
        conn.execute("UPDATE decl_goals SET no=? WHERE id=?", (n, r["id"]))
    recount(conn, g["person_id"])


def add_step(conn, gid: int, title: str, deadline: str = "") -> int:
    g = _goal(conn, gid)
    sid = int(conn.execute("INSERT INTO decl_steps (goal_id, person_id, title, deadline, done) VALUES (?,?,?,?,0)",
                           (gid, g["person_id"], _clean(title, "шаг"), _date(deadline))).lastrowid)
    recount(conn, g["person_id"])
    return sid


def _step(conn, sid: int) -> sqlite3.Row:
    s = conn.execute("SELECT * FROM decl_steps WHERE id=?", (sid,)).fetchone()
    if not s:
        raise DeclError("Шаг не найден.")
    editable(conn, s["person_id"])
    return s


def toggle_step(conn, sid: int, who: str) -> bool:
    s = _step(conn, sid)
    done = not s["done"]
    # отметка ментора — уже проверенная: «отметил сам» (tracker.py) снимается
    conn.execute("UPDATE decl_steps SET done=?, done_by=?, done_at=?, self_marked=0 WHERE id=?",
                 (int(done), who if done else None, _now() if done else None, sid))
    recount(conn, s["person_id"])
    return done


def update_step(conn, sid: int, title: str, deadline: str) -> None:
    s = _step(conn, sid)
    conn.execute("UPDATE decl_steps SET title=?, deadline=? WHERE id=?",
                 (_clean(title, "шаг"), _date(deadline), sid))


def remove_step(conn, sid: int) -> None:
    s = _step(conn, sid)
    conn.execute("DELETE FROM decl_steps WHERE id=?", (sid,))
    recount(conn, s["person_id"])


# ----------------------------------------------------------------- вовлечения

def _inv_text(n: int, when: date | None, program: str, over: bool, planka: int) -> str:
    """Та же строка, что в таблицах ММ7 — её понимают board, посты и лидерборд."""
    parts = [f"Вовлечённый {n}"]
    if when:
        parts.append(f"{when:%d.%m} {program}".strip())
    if over:
        parts.append(f"сверх личной планки {planka}")
    return " · ".join(parts)


def set_planka(conn, pid: int, planka: int) -> None:
    """Личная планка: сколько человек вовлечь. Пустые строки планки добавляются
    или убираются; отмеченные вовлечения не трогаем."""
    editable(conn, pid)
    if not 0 <= planka <= 50:
        raise DeclError("Планка — от 0 до 50 человек.")
    rows = conn.execute("SELECT * FROM involved WHERE person_id=? ORDER BY idx", (pid,)).fetchall()
    done = [r for r in rows if r["done"]]
    conn.execute("DELETE FROM involved WHERE person_id=?", (pid,))
    total = max(planka, len(done))
    for i in range(total):
        if i < len(done):
            txt = re.sub(r" · сверх личной планки \d+", "", done[i]["text"] or "")
            txt = re.sub(r"^Вовлечённый \d+", f"Вовлечённый {i + 1}", txt)
            if i >= planka:
                txt += f" · сверх личной планки {planka}"
            conn.execute("INSERT INTO involved (person_id, idx, text, done) VALUES (?,?,?,1)", (pid, i, txt))
        else:
            conn.execute("INSERT INTO involved (person_id, idx, text, done) VALUES (?,?,?,0)",
                         (pid, i, f"Вовлечённый {i + 1}"))
    recount(conn, pid)


def planka_of(conn, pid: int) -> int:
    rows = conn.execute("SELECT text FROM involved WHERE person_id=?", (pid,)).fetchall()
    return sum(1 for r in rows if "сверх личной планки" not in (r["text"] or ""))


def add_involvement(conn, pid: int, when: str = "", program: str = "большая программа") -> int:
    """Отметить вовлечение: занимает первую свободную строку планки, сверх неё — новая строка."""
    editable(conn, pid)
    day = date.fromisoformat(_date(when)) if (when or "").strip() else date.today()
    planka = planka_of(conn, pid)
    rows = conn.execute("SELECT * FROM involved WHERE person_id=? ORDER BY idx", (pid,)).fetchall()
    free = next((r for r in rows if not r["done"]), None)
    if free:
        idx, over = free["idx"], False
    else:
        idx, over = len(rows), True
    conn.execute("INSERT OR REPLACE INTO involved (person_id, idx, text, done) VALUES (?,?,?,1)",
                 (pid, idx, _inv_text(idx + 1, day, (program or "").strip(), over, planka)))
    recount(conn, pid)
    return idx


def remove_involvement(conn, pid: int, idx: int) -> None:
    """Снять ошибочное вовлечение: строка планки снова пустая, сверх планки — убирается."""
    editable(conn, pid)
    planka = planka_of(conn, pid)
    rows = [r for r in conn.execute("SELECT * FROM involved WHERE person_id=? ORDER BY idx", (pid,))
            if not (r["idx"] == idx and r["done"])]
    done = [r["text"] for r in rows if r["done"]]
    conn.execute("DELETE FROM involved WHERE person_id=?", (pid,))
    for i, txt in enumerate(done):
        conn.execute("INSERT INTO involved (person_id, idx, text, done) VALUES (?,?,?,1)",
                     (pid, i, re.sub(r"^Вовлечённый \d+", f"Вовлечённый {i + 1}", txt)))
    set_planka(conn, pid, planka)


# ----------------------------------------------------------------- деньги

def weeks(f) -> list[dict]:
    """Недели потока: Н1 = со старта по ближайшее воскресенье, дальше пн–вс."""
    if not f or not f["start"] or not f["finish"]:
        return []
    start, finish = date.fromisoformat(f["start"]), date.fromisoformat(f["finish"])
    out, a, n = [], start, 1
    while a <= finish and n <= 20:
        b = min(a + timedelta(days=6 - a.weekday()), finish)
        out.append({"no": n, "from": a, "to": b})
        a, n = b + timedelta(days=1), n + 1
    return out


def money_of(conn, pid: int) -> dict[int, dict]:
    return {r["week"]: dict(r) for r in conn.execute("SELECT * FROM money_weeks WHERE person_id=?", (pid,))}


def set_money(conn, pid: int, week: int, amount: str, who: str) -> None:
    editable(conn, pid)
    digits = re.sub(r"[^\d]", "", amount or "")
    if not digits:
        conn.execute("DELETE FROM money_weeks WHERE person_id=? AND week=?", (pid, week))
    else:
        conn.execute("INSERT OR REPLACE INTO money_weeks (person_id, week, amount, set_by, set_at) "
                     "VALUES (?,?,?,?,?)", (pid, week, int(digits), who, _now()))
    recount(conn, pid)


# ----------------------------------------------------------------- пересчёт

def goal_progress(steps: list, closed: bool) -> float:
    if closed:
        return 1.0
    return sum(1 for s in steps if s["done"]) / len(steps) if steps else 0.0


def recount(conn, pid: int) -> None:
    """Сводки участника — как на листе таблицы: % декларации, цели, шаги, планка, деньги."""
    p = person(conn, pid)
    if not p or p["source"] != "платформа":
        return
    goals = conn.execute("SELECT * FROM decl_goals WHERE person_id=?", (pid,)).fetchall()
    progs, steps_total, steps_done = [], 0, 0
    for g in goals:
        steps = conn.execute("SELECT done FROM decl_steps WHERE goal_id=?", (g["id"],)).fetchall()
        pr = goal_progress(steps, bool(g["closed"]))
        conn.execute("UPDATE decl_goals SET progress=? WHERE id=?", (pr, g["id"]))
        progs.append(pr)
        steps_total += len(steps)
        steps_done += sum(1 for s in steps if s["done"])
    inv = conn.execute("SELECT text, done FROM involved WHERE person_id=?", (pid,)).fetchall()
    planka = sum(1 for r in inv if "сверх личной планки" not in (r["text"] or ""))
    involved = sum(1 for r in inv if r["done"])
    fact = conn.execute("SELECT SUM(amount) FROM money_weeks WHERE person_id=?", (pid,)).fetchone()[0]
    conn.execute(
        "UPDATE people SET progress=?, goals_total=?, goals_closed=?, steps_total=?, steps_done=?, "
        "bar_progress=?, money_fact=?, synced_at=datetime('now') WHERE id=?",
        (sum(progs) / len(progs) if progs else None, len(goals), sum(1 for x in progs if x >= 0.999),
         steps_total, steps_done, involved / planka if planka else None, fact, pid))
    recount_flow(conn, p["flow"])


def recount_flow(conn, flow: str) -> None:
    """Командные итоги по вовлечению для команд на платформе — то, что у ММ7
    берётся с «Дашборда». Выбывший остаётся в своей команде: его вовлечённые
    засчитываются команде, а его планка делится поровну между оставшимися
    участниками этой команды (решение Станислава 15.10.2026). Строки — в том же
    виде, что parse_dashboard: label, kind (участник | выбыл | доля), plan, done."""
    import json
    f = flows.get(conn, name=flow)
    if not f:
        return
    for t in flows.teams(conn, f["id"]):
        if not is_native_team(t):
            continue
        team_id = _legacy_team(conn, flow, t["title"])
        people = conn.execute("SELECT * FROM people WHERE flow=? AND team_id=? ORDER BY name",
                              (flow, team_id)).fetchall()
        active = [p for p in people if p["status"] != "выбыл"]
        rows, plan, done = [], 0.0, 0.0
        for p in people:
            pl = planka_of(conn, p["id"])
            dn = conn.execute("SELECT COUNT(*) FROM involved WHERE person_id=? AND done=1", (p["id"],)).fetchone()[0]
            plan, done = plan + pl, done + dn
            if p["status"] == "выбыл":
                rows.append({"label": f"{p['name']} (выбыл)", "kind": "выбыл", "plan": pl, "done": dn})
                if active and pl:
                    rows.append({"label": f"Планка {p['name']} — на оставшихся: {pl} ÷ {len(active)} = "
                                          f"{pl / len(active):.2f}".replace(".", ",") + " на каждого",
                                 "kind": "доля", "plan": round(pl / len(active), 2), "done": 0})
            else:
                rows.append({"label": p["name"], "kind": "участник", "plan": pl, "done": dn})
        conn.execute("INSERT OR REPLACE INTO team_engagement VALUES (?,?,?,?,?,datetime('now'))",
                     (flow, t["title"], round(plan, 2), done, json.dumps(rows, ensure_ascii=False)))


def share_of(conn, p) -> float:
    """Сколько добавляется к личной планке участника за выбывших его команды."""
    if p["status"] == "выбыл" or not p["team_id"]:
        return 0.0
    mates = conn.execute("SELECT id, status FROM people WHERE flow=? AND team_id=?",
                         (p["flow"], p["team_id"])).fetchall()
    active = sum(1 for m in mates if m["status"] != "выбыл")
    gone = sum(planka_of(conn, m["id"]) for m in mates if m["status"] == "выбыл")
    return gone / active if active else 0.0


def team_dashboard(conn, flow_team_id: int) -> dict[str, Any]:
    """Дашборд команды на платформе — как лист «Дашборд» таблицы."""
    t = conn.execute("SELECT * FROM flow_teams WHERE id=?", (flow_team_id,)).fetchone()
    f = flows.get(conn, t["flow_id"])
    team_id = _legacy_team(conn, f["name"], t["title"])
    people = conn.execute("SELECT * FROM people WHERE flow=? AND team_id=? AND status='в потоке' ORDER BY name",
                          (f["name"], team_id)).fetchall()
    rows = [{"pid": p["id"], "name": p["name"], "goals": p["goals_total"] or 0, "closed": p["goals_closed"] or 0,
             "steps": p["steps_total"] or 0, "done": p["steps_done"] or 0, "progress": p["progress"] or 0,
             "money_goal": p["money_goal"] or 0, "money_fact": p["money_fact"] or 0} for p in people]
    n = len(rows)
    return {"rows": rows,
            "progress": sum(r["progress"] for r in rows) / n if n else 0,
            "goals": sum(r["goals"] for r in rows), "closed": sum(r["closed"] for r in rows),
            "steps": sum(r["steps"] for r in rows), "done": sum(r["done"] for r in rows),
            "money_goal": sum(r["money_goal"] for r in rows), "money_fact": sum(r["money_fact"] for r in rows)}


# ----------------------------------------------------------------- из разбора

def from_review(conn, pid: int, d: dict) -> int:
    """Черновик декларации из разбора (JSON от Claude): цели со сроками, «чем подтвердит»,
    шаги; цель в деньгах, цена слова, плюшка. Только если целей ещё нет — правки
    ментора/организатора не затираем. Возвращает, сколько целей добавлено."""
    p = person(conn, pid)
    if not p or p["source"] != "платформа":
        return 0
    if conn.execute("SELECT 1 FROM decl_goals WHERE person_id=? LIMIT 1", (pid,)).fetchone():
        return 0
    n = 0
    for g in (d.get("goals") or [])[:15]:
        title = str(g.get("title") or "").strip()
        if not title:
            continue
        dl = g.get("deadline") if isinstance(g.get("deadline"), str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", g.get("deadline")) else ""
        gid = add_goal(conn, pid, title[:300], dl or "", str(g.get("proof") or "")[:300])
        for st in (g.get("steps") or [])[:20]:
            t = str(st.get("title") or "").strip()
            if t:
                sd = st.get("deadline") if isinstance(st.get("deadline"), str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", st.get("deadline")) else ""
                add_step(conn, gid, t[:300], sd or "")
        n += 1
    money = d.get("money_goal")
    conn.execute("UPDATE people SET money_goal=COALESCE(money_goal, ?), word_price=COALESCE(word_price, ?), "
                 "perk=COALESCE(perk, ?) WHERE id=?",
                 (int(money) if isinstance(money, (int, float)) and money > 0 else None,
                  (d.get("word_price") or None), (d.get("perk") or None), pid))
    return n


# ----------------------------------------------------------------- на подпись

def snapshot(conn, pid: int) -> dict[str, Any]:
    """Что входит в декларацию на подпись — то, что участник обещает:
    цели со сроками и шагами, чем подтвердит, деньги, цена слова, плюшка.
    Отметки «сделано» не входят — от них подпись не устаревает."""
    p = person(conn, pid)
    goals = []
    for g in conn.execute("SELECT * FROM decl_goals WHERE person_id=? ORDER BY no", (pid,)):
        steps = [{"title": r["title"], "deadline": r["deadline"]} for r in conn.execute(
            "SELECT title, deadline FROM decl_steps WHERE goal_id=? ORDER BY COALESCE(deadline,'9999'), id",
            (g["id"],))]
        goals.append({"no": g["no"], "title": g["title"], "deadline": g["deadline"],
                      "proof": g["proof"], "steps": steps})
    return {"name": p["name"], "flow": p["flow"], "goals": goals,
            "money_goal": p["money_goal"], "word_price": p["word_price"], "perk": p["perk"],
            "planka": planka_of(conn, pid)}


def _hash(snap: dict) -> str:
    import hashlib
    import json
    body = {k: v for k, v in snap.items() if k not in ("name", "flow")}
    return hashlib.sha1(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def versions(conn, pid: int) -> list[dict]:
    import json
    return [dict(r, content=json.loads(r["content"])) for r in conn.execute(
        "SELECT * FROM decl_versions WHERE person_id=? ORDER BY no DESC", (pid,))]


def new_version(conn, pid: int, who: str) -> int:
    """Подготовить к подписи: снимок текущей декларации — новая версия.
    Если с последней версии ничего не поменялось — новую не заводим."""
    import json
    editable(conn, pid)
    snap = snapshot(conn, pid)
    if not snap["goals"]:
        raise DeclError("В декларации нет целей — нечего подписывать.")
    h = _hash(snap)
    last = conn.execute("SELECT * FROM decl_versions WHERE person_id=? ORDER BY no DESC LIMIT 1", (pid,)).fetchone()
    if last and last["hash"] == h and last["status"] != "заменена":
        return last["no"]
    no = (last["no"] if last else 0) + 1
    # неподписанная прошлая версия больше не нужна — её место заняла новая
    conn.execute("UPDATE decl_versions SET status='заменена' WHERE person_id=? AND status='на подписи'", (pid,))
    conn.execute("INSERT INTO decl_versions (person_id, no, content, hash, status, created_by, created_at) "
                 "VALUES (?,?,?,?, 'на подписи', ?, ?)",
                 (pid, no, json.dumps(snap, ensure_ascii=False), h, who, _now()))
    return no


def mark_signed(conn, pid: int, no: int, who: str, on: str = "") -> None:
    editable(conn, pid)
    v = conn.execute("SELECT * FROM decl_versions WHERE person_id=? AND no=?", (pid, no)).fetchone()
    if not v:
        raise DeclError("Такой версии нет.")
    if v["status"] == "заменена":
        raise DeclError("Эта версия уже заменена новой — распечатайте последнюю.")
    # действует последняя подписанная; прежние подписи остаются в истории как «заменена»
    conn.execute("UPDATE decl_versions SET status='заменена' WHERE person_id=? AND status='подписана' AND no<?",
                 (pid, no))
    conn.execute("UPDATE decl_versions SET status='подписана', signed_by=?, signed_on=? WHERE id=?",
                 (who, _date(on) or date.today().isoformat(), v["id"]))


def unmark_signed(conn, pid: int, no: int) -> None:
    editable(conn, pid)
    conn.execute("UPDATE decl_versions SET status='на подписи', signed_by=NULL, signed_on=NULL "
                 "WHERE person_id=? AND no=? AND status='подписана'", (pid, no))


def _d(v) -> str:
    return datetime.fromisoformat(v[:10]).strftime("%d.%m") if v else "без срока"


def diff(old: dict, new: dict) -> list[str]:
    """Что поменялось между двумя снимками — по-человечески, для ментора и печати."""
    out = []
    og = {g["title"]: g for g in old["goals"]}
    ng = {g["title"]: g for g in new["goals"]}
    for t in ng.keys() - og.keys():
        out.append(f"добавлена цель «{t}»")
    for t in og.keys() - ng.keys():
        out.append(f"убрана цель «{t}»")
    for t in ng.keys() & og.keys():
        a, b = og[t], ng[t]
        if (a["deadline"] or "") != (b["deadline"] or ""):
            out.append(f"«{t}»: срок {_d(a['deadline'])} → {_d(b['deadline'])}")
        if (a["proof"] or "") != (b["proof"] or ""):
            out.append(f"«{t}»: подтверждение — «{b['proof'] or '—'}»")
        sa = {s["title"]: s for s in a["steps"]}
        sb = {s["title"]: s for s in b["steps"]}
        plus, minus = sb.keys() - sa.keys(), sa.keys() - sb.keys()
        moved = [x for x in sb.keys() & sa.keys() if (sa[x]["deadline"] or "") != (sb[x]["deadline"] or "")]
        if plus:
            out.append(f"«{t}»: новые шаги — " + "; ".join(sorted(plus)))
        if minus:
            out.append(f"«{t}»: убраны шаги — " + "; ".join(sorted(minus)))
        for x in sorted(moved):
            out.append(f"«{t}»: шаг «{x}» — срок {_d(sa[x]['deadline'])} → {_d(sb[x]['deadline'])}")
    labels = {"money_goal": "цель в деньгах", "word_price": "цена слова", "perk": "плюшка", "planka": "личная планка"}
    for k, lab in labels.items():
        if (old.get(k) or "") != (new.get(k) or ""):
            out.append(f"{lab}: {old.get(k) or '—'} → {new.get(k) or '—'}")
    return out


def sign_state(conn, pid: int) -> dict[str, Any]:
    """Где декларация в цикле подписи: нет версий / ждёт подписи / подписана /
    изменена после подписи (что именно поменялось)."""
    vs = versions(conn, pid)
    signed = next((v for v in vs if v["status"] == "подписана"), None)
    pending = next((v for v in vs if v["status"] == "на подписи"), None)
    cur = snapshot(conn, pid)
    changed = diff(signed["content"], cur) if signed and signed["hash"] != _hash(cur) else []
    if pending and pending["hash"] != _hash(cur):
        state = "правки после печати"     # распечатали, а потом ещё поправили
    elif pending:
        state = "ждёт подписи"
    elif changed:
        state = "нужна новая подпись"
    elif signed:
        state = "подписана"
    else:
        state = "не готовили"
    return {"state": state, "versions": vs, "signed": signed, "pending": pending, "changed": changed,
            "pending_diff": diff(signed["content"], pending["content"]) if signed and pending else []}

