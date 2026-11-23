"""Поток по таблицам деклараций: участники, команды, цели и шаги.

У каждой команды потока своя Google-таблица: лист «Дашборд» и по листу
на участника. Лист участника устроен одинаково (шаблон кураторов):

  колонки A–D, сверху вниз:
    цель декларации — номер в A, название в B, дедлайн в C, выполнение в D;
    под ней шаги — название в B, дедлайн в C, отметка «сделано» в D;
  дальше блоки по меткам в колонке B:
    «ПЛАНКА ПО ВОВЛЕЧЕНИЮ» — командная планка и вовлечённые;
    «Цена слова: …», «Плюшка: …»;
    «ОБРАЗ РЕЗУЛЬТАТА» — чем человек подтвердит каждую цель;
    «ЦЕЛЬ В ДЕНЬГАХ, ₽» и «ИТОГО факт, ₽».

Таблицы открыты по ссылке, поэтому читаются экспортом xlsx без входа.
Участники нового потока ещё не появлялись в собранных чатах — у них нет
телеграм-id, поэтому у карточки свой отрицательный номер (−id в таблице
people). Когда подключим чаты потока, человек свяжется по имени и логину.

Список команд, ссылки и менторы — в JSON рядом с базой (VERSHINA_FLOW_CONFIG).
"""

from __future__ import annotations

import io
import json
import os
import re
import threading
import time
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import store
from .config import APP_DB

CONFIG = Path(os.environ.get("VERSHINA_FLOW_CONFIG", APP_DB.parent / "mm7.json"))
SYNC_EVERY = int(os.environ.get("VERSHINA_FLOW_SYNC", 3600))   # раз в час

SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    flow         TEXT NOT NULL,
    name         TEXT NOT NULL,           -- как лист в таблице: «Фамилия Имя»
    team_id      INTEGER,
    sheet_id     TEXT,
    status       TEXT NOT NULL DEFAULT 'в потоке',   -- в потоке | выбыл
    tg_user_id   INTEGER,                 -- когда свяжем с чатами потока
    username     TEXT,
    progress     REAL,                    -- выполнение декларации, 0…1
    goals_total  INTEGER, goals_closed INTEGER,
    steps_total  INTEGER, steps_done INTEGER,
    money_goal   INTEGER, money_fact INTEGER,
    bar_progress REAL,                    -- планка по вовлечению, 0…1
    word_price   TEXT, perk TEXT,
    synced_at    TEXT,
    UNIQUE (flow, name)
);

CREATE TABLE IF NOT EXISTS decl_goals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id  INTEGER NOT NULL,
    no         INTEGER NOT NULL,
    title      TEXT NOT NULL,
    deadline   TEXT,
    progress   REAL,
    proof      TEXT                       -- «образ результата»: чем подтвердит
);
CREATE INDEX IF NOT EXISTS idx_dgoal_person ON decl_goals(person_id, no);

CREATE TABLE IF NOT EXISTS decl_steps (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id    INTEGER NOT NULL,
    person_id  INTEGER NOT NULL,
    title      TEXT NOT NULL,
    deadline   TEXT,
    done       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_dstep_goal ON decl_steps(goal_id);

CREATE TABLE IF NOT EXISTS involved (
    person_id  INTEGER NOT NULL,
    idx        INTEGER NOT NULL,
    text       TEXT,
    done       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (person_id, idx)
);

CREATE TABLE IF NOT EXISTS flow_sync (
    flow       TEXT PRIMARY KEY,
    at         TEXT,
    ok         INTEGER,
    message    TEXT
);
"""

SKIP_SHEETS = ("дашборд", "шаблон")
# новые пустые листы Google называет «Лист2», «Sheet3» — это не участники (18.10:
# в таблице команды 4 ММ7 такие появились и стали «участниками»)
BLANK_SHEET = re.compile(r"^(лист|sheet)\s*\d*$", re.I)


def _skip(title: str) -> bool:
    return title.lower().startswith(SKIP_SHEETS) or bool(BLANK_SHEET.match(title))


def init(conn) -> None:
    from . import flows
    conn.executescript(SCHEMA)
    flows.init(conn, CONFIG)
    conn.executescript(DASH_SCHEMA)


def config(conn=None, flow: str | None = None) -> dict[str, Any] | None:
    """Поток (по названию, иначе текущий) в форме для сборщика.
    Потоки живут в базе (app/flows.py); mm7.json читается только один раз —
    при переносе в базу."""
    from . import flows
    own = conn is None
    if own:
        conn = store.connect()
    try:
        row = flows.get(conn, name=flow) if flow else flows.current(conn)
        return flows.config(conn, row)
    finally:
        if own:
            conn.close()


def card_id(person_id: int) -> int:
    """Номер карточки: отрицательный, чтобы не пересекаться с телеграм-id."""
    return -int(person_id)


def is_roster(tg_id: int) -> bool:
    return tg_id < 0


# ----------------------------------------------------------------- разбор листа

def _d(v) -> str | None:
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return None


DASH_SCHEMA = """
CREATE TABLE IF NOT EXISTS team_engagement (
    flow       TEXT NOT NULL,
    team_title TEXT NOT NULL,
    plan       REAL,                 -- «Планка» команды из дашборда — её цель по вовлечениям
    done       REAL,                 -- «Вовлечено» команды, с выбывшими и долей за выбывших
    rows       TEXT,                 -- JSON: строки блока, как в дашборде
    synced_at  TEXT,
    PRIMARY KEY (flow, team_title)
);
"""


def parse_dashboard(ws) -> dict | None:
    """Лист «Дашборд»: блок «ПЛАНКА ПО ВОВЛЕЧЕНИЮ — командная цель».
    Итог команды там считают сами организаторы — с выбывшими из команды
    и долей за выбывших из потока; платформа берёт его как есть."""
    rows, inside = [], False
    for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 200), values_only=True):
        cells = [c for c in row if c not in (None, "")]
        if not cells:
            continue
        label = next((_txt(c) for c in cells if isinstance(c, str) and _txt(c)), "")
        up = label.upper()
        if not inside:
            inside = up.startswith("ПЛАНКА ПО ВОВЛЕЧЕНИЮ")
            continue
        if up.startswith("УЧАСТНИК"):
            continue
        nums = [_num(c) for c in cells if _num(c) is not None]
        if up.startswith("КОМАНДА"):
            if len(nums) >= 2:
                return {"plan": nums[0], "done": nums[1], "rows": rows}
            return None
        if up.startswith(("ФИНАНСОВ", "СПРАВКА")) or not label:
            return None          # блок кончился без итоговой строки — не доверяем
        low = label.lower()
        kind = "доля" if low.startswith("за выбывших") else "выбыл" if "выбыл" in low else "участник"
        rows.append({"label": label, "kind": kind,
                     "plan": nums[0] if len(nums) >= 2 else None,
                     "done": nums[1] if len(nums) >= 2 else (nums[0] if nums else None)})
    return None


def dashboard_of(conn, flow: str) -> dict[str, dict]:
    import json as _json
    out = {}
    for r in conn.execute("SELECT * FROM team_engagement WHERE flow=?", (flow,)):
        out[r["team_title"]] = {"plan": r["plan"], "done": r["done"],
                                "rows": _json.loads(r["rows"] or "[]"), "at": r["synced_at"]}
    return out


def _num(v) -> float | None:
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _txt(v) -> str:
    if isinstance(v, (datetime, date)):
        return v.strftime("%d.%m.%Y")      # в строке шага иногда стоит дата, а не текст
    if isinstance(v, float) and v.is_integer():
        v = int(v)                       # шаг «17» в таблице приходит числом 17.0
    return re.sub(r"\s+", " ", str(v)).strip() if v not in (None, False, True) else ""


def parse_sheet(ws) -> dict[str, Any]:
    """Лист участника → цели, шаги, планка, деньги."""
    out: dict[str, Any] = {"goals": [], "involved": [], "proof": {},
                           "progress": _num(ws.cell(1, 4).value)}
    goal = None
    mode = "goals"
    max_row = min(ws.max_row, 400)
    for r in range(4, max_row + 1):
        a, b, c, d = (ws.cell(r, i).value for i in range(1, 5))
        bt = _txt(b)
        up = bt.upper()
        if up.startswith("ПЛАНКА ПО ВОВЛЕЧЕНИЮ"):
            mode = "bar"
            continue
        if up.startswith("ОБРАЗ РЕЗУЛЬТАТА"):
            mode = "proof"
            continue
        if up.startswith("ИТОГО ПО ДЕКЛАРАЦИИ"):
            mode = "totals"
            continue
        if up.startswith("ЦЕЛЬ В ДЕНЬГАХ, ₽"):
            out["money_goal"] = _num(c)
            continue
        if up.startswith("ИТОГО ФАКТ, ₽"):
            out["money_fact"] = _num(c)
            continue
        if mode == "goals":
            if isinstance(a, (int, float)) and not isinstance(a, bool) and bt:
                goal = {"no": int(a), "title": bt, "deadline": _d(c),
                        "progress": _num(d), "steps": []}
                out["goals"].append(goal)
            elif goal is not None and bt and a in (None, ""):
                goal["steps"].append({"title": bt, "deadline": _d(c), "done": d is True})
        elif mode == "bar":
            if bt.lower().startswith("планка по вовлечению"):
                out["bar_progress"] = _num(d)
            elif bt.lower().startswith("вовлечённый") or bt.lower().startswith("вовлеченный"):
                # дату пишут то в текст («· 27.09»), то в соседнюю колонку —
                # из колонки переносим в текст, чтобы вовлечение попало в неделю и ленту
                when = _d(c)
                if when and not re.search(r"\d{1,2}\.\d{1,2}", bt):
                    bt = f"{bt} · {when[8:10]}.{when[5:7]}"
                out["involved"].append({"text": bt, "done": d is True})
            elif bt.lower().startswith("цена слова"):
                out["word_price"] = bt.split(":", 1)[-1].strip() or None
            elif bt.lower().startswith("плюшка"):
                out["perk"] = bt.split(":", 1)[-1].strip() or None
        elif mode == "proof":
            m = re.match(r"Цель\s+(\d+)\.\s*(.*)", bt)
            if m and m.group(2).strip():
                out["proof"][int(m.group(1))] = m.group(2).strip()
        elif mode == "totals":
            low = bt.lower()
            if low.startswith("всего целей") and "—" not in low:
                out["goals_total"] = int(_num(c) or 0)
            elif low.startswith("целей закрыто"):
                out["goals_closed"] = int(_num(c) or 0)
            elif low.startswith("шагов написано"):
                out["steps_total"] = int(_num(c) or 0)
            elif low.startswith("шагов отмечено"):
                out["steps_done"] = int(_num(c) or 0)
    return out


def fetch_book(sheet_id: str):
    import openpyxl
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=xlsx"
    with urllib.request.urlopen(url, timeout=120) as r:
        data = r.read()
    if not data.startswith(b"PK"):
        raise RuntimeError("таблица не открылась по ссылке — проверьте доступ")
    return openpyxl.load_workbook(io.BytesIO(data), data_only=True)


# ----------------------------------------------------------------- синхронизация

def sync(conn) -> str:
    """Перечитать таблицы всех потоков, которые идут или готовятся."""
    from . import flows
    # 4-дневный мастермайнд читает свою таблицу участников сам (app/fourday.py)
    msgs = [sync_flow(conn, flows.config(conn, f)) for f in flows.syncable(conn)
            if ("kind" not in f.keys() or f["kind"] != "4 дня")]
    return "; ".join(msgs) or "нет потоков, которые идут или готовятся"


_SYNC_LOCK = threading.Lock()


def sync_flow(conn, cfg: dict | None) -> str:
    """Перечитать таблицы одного потока. Ежечасное обновление и кнопка
    «Обновить сейчас» не должны писать в базу одновременно — отсюда замок."""
    if not cfg:
        return "поток не найден"
    from .proclock import held
    with _SYNC_LOCK, held("roster-sync"):
        return _sync_flow(conn, cfg)


def _sync_flow(conn, cfg: dict) -> str:
    flow = cfg["flow"]
    # команды без таблицы ведутся на платформе (app/decl.py) — их не читаем
    cfg = dict(cfg, teams=[t for t in cfg["teams"] if t["sheet"]])
    if not cfg["teams"]:
        msg = "в потоке нет команд с таблицами — всё ведётся на платформе"
        conn.execute("INSERT OR REPLACE INTO flow_sync (flow, at, ok, message) "
                     "VALUES (?, datetime('now'), 1, ?)", (flow, msg))
        return f"{flow}: {msg}"
    try:
        # 1. Скачать и разобрать все таблицы — долго (сеть), база не занята.
        parsed = []
        for t in cfg["teams"]:
            book = fetch_book(t["sheet"])
            sheets, dash = [], None
            for ws in book.worksheets:
                title = ws.title.strip()
                if title.lower().startswith("дашборд"):
                    dash = parse_dashboard(ws)
                if _skip(title):
                    continue
                left = title.lower().startswith("выбыл")
                name = re.sub(r"^выбыл[а-я]*[_\s-]*", "", title, flags=re.I).strip()
                sheets.append((name, left, parse_sheet(ws)))
            parsed.append((t, sheets, dash))
    except Exception as exc:
        return _sync_failed(conn, flow, exc)

    # 2. Записать одной транзакцией: сотни мелких записей по отдельности
    # держали базу ~10 с, и кнопки на страницах падали с «database is locked».
    # Заодно поток обновляется целиком или никак.
    people_seen = 0
    seen: list[int] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        for t, sheets, dash in parsed:
            team_id = _ensure_team(conn, flow, t)
            if dash:
                conn.execute("INSERT OR REPLACE INTO team_engagement VALUES (?,?,?,?,?,datetime('now'))",
                             (flow, t["title"], dash["plan"], dash["done"],
                              __import__("json").dumps(dash["rows"], ensure_ascii=False)))
            else:
                conn.execute("DELETE FROM team_engagement WHERE flow=? AND team_title=?", (flow, t["title"]))
            members = [_save_person(conn, flow, name, team_id, t["sheet"], left, data)
                       for name, left, data in sheets]
            people_seen += len(members)
            seen += members
            _set_team_members(conn, team_id, members, t)
        _forget_missing(conn, flow, seen)
        linked = link_people(conn, flow)
        msg = f"обновлено: {people_seen} участников из {len(cfg['teams'])} таблиц"
        if linked:
            msg += f", связано с телеграмом: {linked}"
        conn.execute("INSERT OR REPLACE INTO flow_sync (flow, at, ok, message) "
                     "VALUES (?, datetime('now'), 1, ?)", (flow, msg))
        conn.execute("COMMIT")
        return f"{flow}: {msg}"
    except Exception as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        return _sync_failed(conn, flow, exc)


def _sync_failed(conn, flow: str, exc: Exception) -> str:
    msg = f"не удалось обновить: {exc}"
    conn.execute("INSERT OR REPLACE INTO flow_sync (flow, at, ok, message) "
                 "VALUES (?, datetime('now'), 0, ?)", (flow, msg[:500]))
    return f"{flow}: {msg}"


def _forget_missing(conn, flow: str, seen: list[int]) -> None:
    """Кого больше нет ни в одной таблице потока (команду убрали, лист удалили) —
    пометить «нет в таблицах» и снять с менторов. Из базы не удаляем:
    к человеку могут быть привязаны разборы и сведения."""
    marks = ",".join("?" * len(seen)) or "NULL"
    gone = [r["id"] for r in conn.execute(
        f"SELECT id FROM people WHERE flow=? AND source<>'платформа' AND id NOT IN ({marks})", (flow, *seen))]
    for pid in gone:
        conn.execute("UPDATE people SET status='нет в таблицах', team_id=NULL WHERE id=?", (pid,))
        conn.execute("DELETE FROM assignments WHERE tg_user_id=?", (card_id(pid),))
        conn.execute("DELETE FROM team_members WHERE tg_user_id=?", (card_id(pid),))


def check_sheet(sheet_id: str) -> dict[str, Any]:
    """Проверить таблицу до того, как добавить её в поток: открывается ли
    по ссылке, есть ли листы участников и похожи ли они на декларацию."""
    import urllib.error
    try:
        book = fetch_book(sheet_id)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"ok": False, "error": "Таблица закрыта. Откройте доступ «Все, у кого есть ссылка» — "
                                          "«Читатель» и проверьте снова."}
        if e.code == 404:
            return {"ok": False, "error": "Такой таблицы нет — проверьте ссылку."}
        return {"ok": False, "error": f"Google ответил ошибкой {e.code}. Попробуйте ещё раз."}
    except RuntimeError as e:
        return {"ok": False, "error": "Таблица не открылась по ссылке. Откройте доступ «Все, у кого есть "
                                      "ссылка» — «Читатель» и проверьте снова."}
    except Exception as e:
        return {"ok": False, "error": f"Не удалось прочитать таблицу: {e}"}
    people, skipped, warnings = [], [], []
    for ws in book.worksheets:
        title = ws.title.strip()
        if _skip(title):
            skipped.append(title)
            continue
        d = parse_sheet(ws)
        left = title.lower().startswith("выбыл")
        name = re.sub(r"^выбыл[а-я]*[_\s-]*", "", title, flags=re.I).strip()
        people.append({"name": name, "left": left, "goals": len(d["goals"]),
                       "steps": sum(len(g["steps"]) for g in d["goals"]),
                       "progress": d.get("progress"), "money": d.get("money_goal")})
        if not d["goals"] and not left:
            warnings.append(f"На листе «{title}» не нашлось целей — если это участник, "
                            f"проверьте, что лист по шаблону декларации.")
    if not people:
        return {"ok": False, "error": "В таблице нет листов участников — только "
                                      + (", ".join(skipped) or "пустые листы") + "."}
    return {"ok": True, "people": people, "skipped": skipped, "warnings": warnings}


def _key(text: str) -> frozenset:
    return frozenset(w for w in re.findall(r"[а-яёa-z]+", (text or "").lower().replace("ё", "е")) if len(w) > 1)


def link_people(conn, flow: str) -> int:
    """Связать участника из таблицы с его телеграмом — по имени и фамилии.

    В таблице «Фамилия Имя», в телеграме часто «Имя Фамилия» — сравниваем
    набором слов. Связываем только при единственном совпадении: тёзки
    остаются несвязанными, лучше без связи, чем с чужой перепиской.
    """
    from .config import KB_DB
    import sqlite3
    try:
        kb = sqlite3.connect(f"file:{KB_DB}?mode=ro", uri=True)
        users = kb.execute("SELECT id, first_name, last_name, username FROM users "
                           "WHERE COALESCE(is_bot,0)=0").fetchall()
    except sqlite3.Error:
        return 0
    index: dict[frozenset, list] = {}
    for uid, first, last, username in users:
        k = _key(f"{first or ''} {last or ''}")
        if len(k) >= 2:
            index.setdefault(k, []).append((uid, username))
    n = 0
    for p in conn.execute("SELECT id, name FROM people WHERE flow=? AND tg_user_id IS NULL",
                          (flow,)).fetchall():
        hits = index.get(_key(p["name"]), [])
        if len(hits) == 1:
            conn.execute("UPDATE people SET tg_user_id=?, username=? WHERE id=?",
                         (hits[0][0], hits[0][1], p["id"]))
            n += 1
    return n


def _ensure_team(conn, flow: str, t: dict) -> int:
    row = conn.execute("SELECT id FROM teams WHERE flow=? AND title=?",
                       (flow, t["title"])).fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO teams (title, flow, goal, note) VALUES (?,?,0,?)",
                       (t["title"], flow, "из таблиц деклараций"))
    return int(cur.lastrowid)


def _save_person(conn, flow, name, team_id, sheet_id, left, d) -> int:
    row = conn.execute("SELECT id FROM people WHERE flow=? AND name=?", (flow, name)).fetchone()
    vals = dict(team_id=team_id, sheet_id=sheet_id, status="выбыл" if left else "в потоке",
                progress=d.get("progress"), goals_total=d.get("goals_total"),
                goals_closed=d.get("goals_closed"), steps_total=d.get("steps_total"),
                steps_done=d.get("steps_done"),
                money_goal=int(d["money_goal"]) if d.get("money_goal") else None,
                money_fact=int(d["money_fact"]) if d.get("money_fact") else None,
                bar_progress=d.get("bar_progress"), word_price=d.get("word_price"),
                perk=d.get("perk"))
    if row:
        pid = row["id"]
        conn.execute("UPDATE people SET " + ", ".join(f"{k}=?" for k in vals) +
                     ", synced_at=datetime('now') WHERE id=?", (*vals.values(), pid))
    else:
        cur = conn.execute(
            "INSERT INTO people (flow, name, " + ", ".join(vals) + ", synced_at) VALUES (?,?," +
            ",".join("?" * len(vals)) + ", datetime('now'))", (flow, name, *vals.values()))
        pid = int(cur.lastrowid)

    # цели и шаги — полностью из таблицы: правят их сами участники и кураторы там
    old = [r["id"] for r in conn.execute("SELECT id FROM decl_goals WHERE person_id=?", (pid,))]
    if old:
        conn.execute(f"DELETE FROM decl_steps WHERE goal_id IN ({','.join('?'*len(old))})", old)
    conn.execute("DELETE FROM decl_goals WHERE person_id=?", (pid,))
    for g in d["goals"]:
        cur = conn.execute(
            "INSERT INTO decl_goals (person_id, no, title, deadline, progress, proof) "
            "VALUES (?,?,?,?,?,?)",
            (pid, g["no"], g["title"], g["deadline"], g["progress"], d["proof"].get(g["no"])))
        gid = int(cur.lastrowid)
        for s in g["steps"]:
            conn.execute("INSERT INTO decl_steps (goal_id, person_id, title, deadline, done) "
                         "VALUES (?,?,?,?,?)", (gid, pid, s["title"], s["deadline"], int(s["done"])))
    conn.execute("DELETE FROM involved WHERE person_id=?", (pid,))
    for i, inv in enumerate(d["involved"]):
        conn.execute("INSERT INTO involved (person_id, idx, text, done) VALUES (?,?,?,?)",
                     (pid, i, inv["text"], int(inv["done"])))
    return pid


def _set_team_members(conn, team_id: int, pids: list[int], t: dict) -> None:
    """Состав команды и закрепление: менторы и трекер видят своих участников."""
    conn.execute("DELETE FROM team_members WHERE team_id=? AND tg_user_id < 0", (team_id,))
    for pid in pids:
        conn.execute("INSERT OR IGNORE INTO team_members (team_id, tg_user_id, role) "
                     "VALUES (?,?, 'участник')", (team_id, card_id(pid)))
    # состав менторов мог поменяться — закрепления команды собираем заново
    marks = ",".join("?" * len(pids))
    if pids:
        conn.execute(f"DELETE FROM assignments WHERE tg_user_id IN ({marks})",
                     [card_id(p) for p in pids])
    conn.execute("DELETE FROM team_members WHERE team_id=? AND role='ментор'", (team_id,))
    for login in t.get("mentors", []) + t.get("trackers", []):
        u = conn.execute("SELECT id, tg_user_id FROM users WHERE login=?", (login.lower(),)).fetchone()
        if not u:
            continue
        if u["tg_user_id"]:
            conn.execute("INSERT OR IGNORE INTO team_members (team_id, tg_user_id, role) "
                         "VALUES (?,?, 'ментор')", (team_id, u["tg_user_id"]))
        for pid in pids:
            conn.execute("INSERT OR IGNORE INTO assignments (mentor_id, tg_user_id) VALUES (?,?)",
                         (u["id"], card_id(pid)))


def start_background() -> None:
    """Обновлять таблицы раз в час, пока работает приложение."""

    def loop():
        while True:
            conn = store.connect()
            try:
                sync(conn)
            finally:
                conn.close()
            time.sleep(SYNC_EVERY)

    threading.Thread(target=loop, daemon=True, name="flow-sync").start()


# ----------------------------------------------------------------- для страниц

def person(conn, tg_id: int):
    if not is_roster(tg_id):
        return None
    return conn.execute(
        "SELECT p.*, t.title AS team_title, "
        # ссылка на страницу команды в «Командах» — по команде потока
        "(SELECT ft.id FROM flow_teams ft JOIN flows f ON f.id=ft.flow_id "
        " WHERE f.name=p.flow AND ft.title=t.title) AS team_ref FROM people p "
        "LEFT JOIN teams t ON t.id = p.team_id WHERE p.id=?", (-tg_id,)).fetchone()


def goals_of(conn, person_id: int) -> list[dict]:
    today = date.today().isoformat()
    goals = []
    for g in conn.execute("SELECT * FROM decl_goals WHERE person_id=? ORDER BY no", (person_id,)):
        steps = [dict(s) for s in conn.execute(
            "SELECT * FROM decl_steps WHERE goal_id=? ORDER BY COALESCE(deadline,'9999'), id",
            (g["id"],))]
        for s in steps:
            s["late"] = bool(not s["done"] and s["deadline"] and s["deadline"] < today)
        late = sum(1 for s in steps if s["late"])
        prog = g["progress"] or 0
        if prog >= 0.999:
            state = "закрыта"
        elif g["deadline"] and g["deadline"] < today:
            state = "просрочена"
        elif late:
            state = "отстаёт"
        else:
            state = "в работе"
        goals.append(dict(g, steps=steps, late=late, state=state))
    return goals


def involved_of(conn, person_id: int) -> list:
    return conn.execute("SELECT * FROM involved WHERE person_id=? ORDER BY idx", (person_id,)).fetchall()


def roster(conn, flow: str | None = None, ids: list[int] | None = None) -> list[dict]:
    """Участники потока для списков: с командой, прогрессом и отставанием."""
    today = date.today().isoformat()
    sql = ("SELECT p.*, t.title AS team_title FROM people p LEFT JOIN teams t ON t.id=p.team_id")
    rows = conn.execute(sql + " ORDER BY t.title, p.status, p.name").fetchall()
    late = {r["person_id"]: r["n"] for r in conn.execute(
        "SELECT person_id, COUNT(*) AS n FROM decl_steps WHERE done=0 AND deadline < ? "
        "GROUP BY person_id", (today,))}
    out = []
    for r in rows:
        if flow and r["flow"] != flow:
            continue
        cid = card_id(r["id"])
        if ids is not None and cid not in ids:
            continue
        out.append(dict(r, card=cid, late_steps=late.get(r["id"], 0)))
    return out


def last_sync(conn, flow: str):
    return conn.execute("SELECT * FROM flow_sync WHERE flow=?", (flow,)).fetchone()


# ----------------------------------------------------------------- обзор потока

def flow_overview(conn, ids: list[int] | None = None, flow: str | None = None) -> dict[str, Any] | None:
    """Текущий поток для страницы «Обзор»: цифры, команды, кому нужна помощь
    и дедлайны недели. Только по декларациям текущего потока — закончившиеся
    потоки сюда не попадают. ids — карточки, видимые роли (None — все)."""
    cfg = config(conn, flow)
    if not cfg:
        return None
    flow = cfg.get("flow", "")
    today = date.today()
    t_iso = today.isoformat()
    people = [p for p in roster(conn, flow, ids) if p["status"] == "в потоке"]
    if not people:
        return {"flow": flow, "people": [], "teams": [], "help": [], "week": [], "dots": [],
                "days": {}, "totals": None, "sync": last_sync(conn, flow)}
    pids = [p["id"] for p in people]
    marks = ",".join("?" * len(pids))
    by_id = {p["id"]: p for p in people}

    # Даты потока — из mm7.json, если заданы; иначе только последний дедлайн
    start = cfg.get("start")
    end = cfg.get("end")
    last_deadline = conn.execute(
        f"SELECT MAX(deadline) FROM decl_goals WHERE person_id IN ({marks})", pids).fetchone()[0]
    days = {}
    if start and date.fromisoformat(start) > today:
        # поток ещё не стартовал: «день −27» никому ничего не говорит
        days["until"] = (date.fromisoformat(start) - today).days
        start = None
    if start:
        days["day"] = (today - date.fromisoformat(start)).days + 1
    if end:
        days["left"] = (date.fromisoformat(end) - today).days
        if start:
            total = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
            days["total"] = total
            days["share"] = max(0.0, min(1.0, days.get("day", 0) / total))

    def avg(vals):
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None

    totals = {
        "people": len(people),
        "progress": avg(p["progress"] for p in people),
        "bar": avg(p["bar_progress"] for p in people),
        "money_goal": sum(p["money_goal"] or 0 for p in people),
        "money_fact": sum(p["money_fact"] or 0 for p in people),
        "steps_total": sum(p["steps_total"] or 0 for p in people),
        "steps_done": sum(p["steps_done"] or 0 for p in people),
        "late": sum(p["late_steps"] for p in people),
        "goals_total": sum(p["goals_total"] or 0 for p in people),
        "goals_closed": sum(p["goals_closed"] or 0 for p in people),
    }

    # Команды: менторы — по логинам из конфига
    names = {r["login"]: r["name"] for r in conn.execute("SELECT login, name FROM users")}
    cfg_teams = {t["title"]: t for t in cfg.get("teams", [])}
    teams: dict[str, dict] = {}
    for p in people:
        teams.setdefault(p["team_title"] or "Без команды", []).append(p)
    team_cards = []
    for title, members in teams.items():
        t = cfg_teams.get(title, {})
        short = title.split("—")[0].strip() if "—" in title else title
        team_cards.append({
            "title": short, "full": title, "no": t.get("no"),
            "members": len(members),
            "progress": avg(m["progress"] for m in members),
            "bar": avg(m["bar_progress"] for m in members),
            "money_goal": sum(m["money_goal"] or 0 for m in members),
            "money_fact": sum(m["money_fact"] or 0 for m in members),
            "late": sum(m["late_steps"] for m in members),
            "mentors": [names.get(l.lower(), l) for l in t.get("mentors", [])],
        })
    team_cards.sort(key=lambda c: (c["no"] is None, c["no"] or 0, c["title"]))

    # Кому нужна помощь: просроченные шаги и цели, пустая декларация
    late_goals = {}
    for r in conn.execute(
            f"SELECT person_id, COUNT(*) n FROM decl_goals WHERE person_id IN ({marks}) "
            f"AND deadline < ? AND COALESCE(progress,0) < 0.999 GROUP BY person_id",
            (*pids, t_iso)):
        late_goals[r["person_id"]] = r["n"]
    oldest = {}
    for r in conn.execute(
            f"SELECT person_id, MIN(deadline) d, title FROM decl_steps WHERE person_id IN ({marks}) "
            f"AND done=0 AND deadline < ? GROUP BY person_id", (*pids, t_iso)):
        oldest[r["person_id"]] = (r["d"], r["title"])
    # отчёты из чата отчётов: кто пропускает несколько дней подряд
    from . import flows as _flows, reports as _reports
    _f = _flows.get(conn, name=flow)
    rep = _reports.history(conn, _f) if _f else {"has_chat": False, "stats": {}, "counts": {}, "total": 0}
    help_ = []
    for p in people:
        reasons, weight = [], 0
        miss = (rep["stats"].get(p["card"]) or {}).get("miss", 0)
        if miss >= 2:
            reasons.append(f"нет отчёта {miss} {_plural(miss, 'день', 'дня', 'дней')} подряд")
            weight += 3 * miss
        if late_goals.get(p["id"]):
            n = late_goals[p["id"]]
            reasons.append(f"{n} {_plural(n, 'цель просрочена', 'цели просрочены', 'целей просрочено')}")
            weight += 10 * n
        if p["late_steps"]:
            n = p["late_steps"]
            reasons.append(f"{n} {_plural(n, 'шаг просрочен', 'шага просрочено', 'шагов просрочено')}")
            weight += n
        if not p["goals_total"]:
            reasons.append("декларация пустая")
            weight += 5
        if not reasons:
            continue
        old = oldest.get(p["id"])
        help_.append({
            "card": p["card"], "name": p["name"], "team": (p["team_title"] or "").split("—")[0].strip(),
            "progress": p["progress"], "reasons": reasons, "weight": weight,
            "since": (today - date.fromisoformat(old[0])).days if old else None,
            "step": old[1] if old else None,
        })
    help_.sort(key=lambda h: (-h["weight"], -(h["since"] or 0)))

    # Календарь: дедлайны деклараций по дням, справа — план выбранного дня
    cal = flow_calendar(conn, pids, cfg.get("start"), cfg.get("end"),
                        {d.isoformat(): (n, rep["total"]) for d, n in rep["counts"].items()})
    pick = t_iso if any(c["date"] == t_iso and c["in"] for m in cal for w in m["weeks"] for c in w) \
        else next((c["date"] for m in cal for w in m["weeks"] for c in w if c["in"] and c["n"]), None)
    plan = day_plan(conn, flow, ids, pick) if pick else None

    return {
        "flow": flow, "days": days, "last_deadline": last_deadline, "totals": totals,
        "teams": team_cards, "help": help_, "cal": cal, "pick": pick, "plan": plan,
        # для шапки: каждый участник — частица, глубина по выполнению декларации
        # по клику на частицу — кто это (только видимые роли участники)
        "dots": [{"p": round(p["progress"] or 0, 3),
                  "t": next((i for i, c in enumerate(team_cards) if c["full"] == (p["team_title"] or "Без команды")), 0),
                  "n": " ".join(reversed(p["name"].split())) if len(p["name"].split()) == 2 else p["name"],
                  "c": p["card"], "team": (p["team_title"] or "").split("—")[0].strip()}
                 for p in people],
        "sync": last_sync(conn, flow),
    }


_WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
_MONTHS = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль",
           "август", "сентябрь", "октябрь", "ноябрь", "декабрь")
_MONTHS_GEN = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
               "августа", "сентября", "октября", "ноября", "декабря")
_WEEKDAYS_FULL = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")


def flow_calendar(conn, pids: list[int], start: str | None, end: str | None,
                  reps: dict[str, tuple[int, int]] | None = None) -> list[dict]:
    """Месяцы потока сеткой с понедельника: в каждом дне — сколько шагов и целей
    стоит на этот день, сколько сделано и сколько уже просрочено.
    Границы — даты потока, а если их нет — первый и последний дедлайн."""
    from calendar import Calendar
    if not pids:
        return []
    marks = ",".join("?" * len(pids))
    today = date.today().isoformat()
    per: dict[str, dict] = {}
    for r in conn.execute(
            f"SELECT deadline d, COUNT(*) n, SUM(done) ok FROM decl_steps "
            f"WHERE person_id IN ({marks}) AND deadline IS NOT NULL GROUP BY deadline", pids):
        per[r["d"]] = {"n": r["n"], "ok": r["ok"] or 0, "goals": 0}
    for r in conn.execute(
            f"SELECT deadline d, COUNT(*) n FROM decl_goals "
            f"WHERE person_id IN ({marks}) AND deadline IS NOT NULL GROUP BY deadline", pids):
        per.setdefault(r["d"], {"n": 0, "ok": 0, "goals": 0})["goals"] = r["n"]
    if not per and not (start and end):
        return []
    lo = start or min(per)
    hi = end or max(per)
    if hi < lo:
        lo, hi = hi, lo
    top = max((v["n"] for k, v in per.items() if lo <= k <= hi), default=0) or 1
    out = []
    y, m = int(lo[:4]), int(lo[5:7])
    while (y, m) <= (int(hi[:4]), int(hi[5:7])) and len(out) < 6:
        weeks = []
        for wk in Calendar().monthdatescalendar(y, m):
            row = []
            for d in wk:
                iso = d.isoformat()
                v = per.get(iso, {"n": 0, "ok": 0, "goals": 0})
                row.append({
                    "date": iso, "day": d.day, "own": d.month == m, "in": lo <= iso <= hi,
                    "today": iso == today, "past": iso < today,
                    "n": v["n"], "ok": v["ok"], "goals": v["goals"],
                    "late": v["n"] - v["ok"] if iso < today else 0,
                    # насыщенность клетки: корень, чтобы дни с 5 шагами не терялись рядом со 100
                    "load": round((v["n"] / top) ** 0.5, 2) if v["n"] else 0,
                    # отчёты за прошедший день: доля сдавших — для отметки в клетке
                    "rep": (round(reps[iso][0] / reps[iso][1], 2) if reps and iso in reps and reps[iso][1] else None),
                })
            weeks.append(row)
        out.append({"key": f"{y}-{m:02d}", "title": f"{_MONTHS[m - 1]} {y}", "weeks": weeks,
                    "has_today": any(c["today"] and c["own"] for w in weeks for c in w)})
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def day_plan(conn, flow: str, ids: list[int] | None, day: str) -> dict:
    """Что у участников стоит на день: по человеку — цели и шаги с отметкой
    «сделано» и «просрочено». Видимость — как у роли (ids)."""
    d = date.fromisoformat(day)
    today = date.today().isoformat()
    people = {p["id"]: p for p in roster(conn, flow, ids) if p["status"] == "в потоке"}
    by: dict[int, dict] = {}
    if people:
        marks = ",".join("?" * len(people))
        rows = conn.execute(
            f"SELECT person_id, title, 'цель' kind, COALESCE(progress,0) >= 0.999 done FROM decl_goals "
            f"WHERE person_id IN ({marks}) AND deadline=? "
            f"UNION ALL SELECT person_id, title, 'шаг', done FROM decl_steps "
            f"WHERE person_id IN ({marks}) AND deadline=?",
            (*people, day, *people, day)).fetchall()
        for r in rows:
            p = people[r["person_id"]]
            e = by.setdefault(p["id"], {
                "card": p["card"], "name": p["name"],
                "team": (p["team_title"] or "").split("—")[0].strip(),
                "goals": [], "steps": []})
            item = {"title": r["title"], "done": bool(r["done"]),
                    "late": not r["done"] and day < today}
            e["goals" if r["kind"] == "цель" else "steps"].append(item)
    people_out = sorted(by.values(), key=lambda e: (e["team"], e["name"]))
    steps = [s for e in people_out for s in e["steps"]]
    # отчёты за этот день — из чата отчётов (только прошедшие дни)
    reps = None
    from . import flows as _flows, reports as _reports
    _f = _flows.get(conn, name=flow)
    if _f and day < today and _reports.report_chats(conn, _f["id"]):
        r = _reports.day_report(conn, _f, d, ids)
        if r["total"] and not r["day_off"]:
            reps = {"done": r["done"], "total": r["total"],
                    "missing": [{"card": x["card"], "name": x["name"]} for x in r["missing"]]}
    return {
        "date": day, "today": day == today, "past": day < today,
        "title": f"{d.day} {_MONTHS_GEN[d.month - 1]}", "weekday": _WEEKDAYS_FULL[d.weekday()],
        "people": people_out, "reports": reps,
        "n_steps": len(steps), "done": sum(s["done"] for s in steps),
        "n_goals": sum(len(e["goals"]) for e in people_out),
    }


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    n %= 10
    return one if n == 1 else few if 2 <= n <= 4 else many


def flow_details(conn, ids: list[int] | None = None, flow: str | None = None) -> dict[str, Any] | None:
    """Для страницы «Поток»: шаги по неделям (план, сделано, хвост),
    распределение участников по выполнению и деньги по людям."""
    from datetime import timedelta
    cfg = config(conn, flow)
    if not cfg:
        return None
    flow = cfg.get("flow", "")
    people = [p for p in roster(conn, flow, ids) if p["status"] == "в потоке"]
    if not people:
        return None
    pids = [p["id"] for p in people]
    marks = ",".join("?" * len(pids))
    today = date.today()

    # Шаги по неделям дедлайна: сколько было запланировано и сколько сделано
    weeks: dict[str, dict] = {}
    for r in conn.execute(
            f"SELECT deadline, done FROM decl_steps WHERE person_id IN ({marks}) "
            f"AND deadline IS NOT NULL", pids):
        try:
            d = date.fromisoformat(r["deadline"][:10])
        except ValueError:
            continue
        monday = d - timedelta(days=d.weekday())
        w = weeks.setdefault(monday.isoformat(), {"monday": monday, "total": 0, "done": 0})
        w["total"] += 1
        w["done"] += r["done"]
    week_list = []
    for key in sorted(weeks):
        w = weeks[key]
        sunday = w["monday"] + timedelta(days=6)
        w.update(label=f"{w['monday']:%d.%m}–{sunday:%d.%m}",
                 now=w["monday"] <= today <= sunday, future=w["monday"] > today,
                 open=w["total"] - w["done"])
        week_list.append(w)
    top = max((w["total"] for w in week_list), default=1) or 1

    # Распределение по выполнению декларации
    buckets = [{"label": f"{lo}–{lo + 20}%", "lo": lo, "people": []} for lo in range(0, 100, 20)]
    for p in people:
        v = min(99.9, max(0.0, (p["progress"] or 0) * 100))
        buckets[int(v // 20)]["people"].append(p)
    btop = max(len(b["people"]) for b in buckets) or 1

    money = sorted(
        [dict(p, share=(p["money_fact"] or 0) / p["money_goal"]) for p in people if p["money_goal"]],
        key=lambda p: -p["share"])

    return {"weeks": week_list, "weeks_top": top, "buckets": buckets, "buckets_top": btop,
            "money": money}
