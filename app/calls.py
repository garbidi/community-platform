"""Созвоны: встречи «Вершины» из Стенографа.

Стенограф кладёт готовые встречи с меткой «Вершина» в свою папку выдачи
(data/export/calls/<id>/: meta.json, transcript.json, summary.md,
speakers.json), она подключена сюда на чтение как /stenograf. Личные
встречи Глеба туда не попадают вовсе — фильтровать здесь нечего.

Говорящие в Зуме подписаны как попало («Azat_Kuznetsov_Business»,
«Roman Sidorenko», «iPhone (Роман)»). Сопоставляем с участниками потоков
и командой: по имени и фамилии в любом порядке, кириллицей и латиницей,
по нику. Чего не узнали — организатор отмечает руками (таблица
call_aliases), и это действует во всех встречах.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("VERSHINA_STENOGRAF_DIR", "/stenograf")) / "calls"

SCHEMA = """
CREATE TABLE IF NOT EXISTS call_aliases (
    alias   TEXT NOT NULL,          -- подпись говорящего, нормализованная (norm())
    call_id TEXT NOT NULL DEFAULT '',  -- '' — во всех встречах; иначе только в этой
    label   TEXT NOT NULL,          -- как подписан в Зуме, для показа
    kind    TEXT NOT NULL,          -- участник | команда | чужой
    ref     INTEGER,                -- карточка участника или id пользователя
    set_by  TEXT, set_at TEXT,
    PRIMARY KEY (alias, call_id)
);
CREATE TABLE IF NOT EXISTS call_guesses (
    call_id    TEXT NOT NULL,
    label      TEXT NOT NULL,       -- подпись в Зуме, как есть
    kind       TEXT, ref INTEGER,   -- кого предлагает Claude; NULL — не определить
    confidence TEXT,                -- высокая | средняя | низкая
    evidence   TEXT,                -- JSON: цитаты с временем
    why        TEXT,
    status     TEXT NOT NULL DEFAULT 'предложено',   -- предложено | принято | отклонено
    created_at TEXT,
    PRIMARY KEY (call_id, label)
);
CREATE TABLE IF NOT EXISTS call_guess_runs (
    call_id TEXT PRIMARY KEY,
    status  TEXT NOT NULL,          -- идёт | готово | ошибка
    note    TEXT, at TEXT
);
CREATE TABLE IF NOT EXISTS call_flow (
    call_id TEXT PRIMARY KEY,       -- папка встречи в Стенографе
    flow    TEXT,                   -- имя потока; NULL — «вне потоков»
    set_by  TEXT, set_at TEXT
);
"""


def init(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(call_aliases)")}
    if cols and "call_id" not in cols:
        # отметки бывают на одну встречу («iPhone», «Голос 3») — ключ (подпись, встреча)
        conn.execute("ALTER TABLE call_aliases RENAME TO call_aliases_old")
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO call_aliases (alias, call_id, label, kind, ref, set_by, set_at) "
                     "SELECT alias, '', label, kind, ref, set_by, set_at FROM call_aliases_old")
        conn.execute("DROP TABLE call_aliases_old")
    conn.executescript(SCHEMA)
    # перезапуск посреди разбора — не висеть в «идёт»
    conn.execute("UPDATE call_guess_runs SET status='ошибка', note='прервано перезапуском' WHERE status='идёт'")


# ----------------------------------------------------------------- чтение

def _read(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


_CACHE: dict[str, tuple[str, dict]] = {}


def _load(folder: Path) -> dict | None:
    meta = _read(folder / "meta.json")
    if not meta:
        return None
    hit = _CACHE.get(folder.name)
    if hit and hit[0] == meta.get("src"):
        return hit[1]
    tr = _read(folder / "transcript.json", {}) or {}
    turns = tr.get("turns") or []
    talk: dict[str, dict] = {}
    for t in turns:
        who = t.get("who") or "Без подписи"
        s = talk.setdefault(who, {"who": who, "secs": 0.0, "turns": 0})
        s["secs"] += max(0.0, (t.get("end") or 0) - (t.get("start") or 0))
        s["turns"] += 1
    try:
        summary = (folder / "summary.md").read_text(encoding="utf-8")
    except OSError:
        summary = None
    started = meta.get("rec_started") or meta.get("created")
    try:
        when = datetime.fromisoformat(started) if started else None
    except ValueError:
        when = None
    call = {
        "id": folder.name, "topic": meta.get("topic") or "", "when": when,
        "docx": (folder / "meeting.docx").is_file(),
        "secs": meta.get("rec_seconds") or (turns[-1].get("end") if turns else 0) or 0,
        "platform": meta.get("platform_title") or "", "turns": turns,
        "talk": sorted(talk.values(), key=lambda s: -s["secs"]), "summary": summary,
    }
    _CACHE[folder.name] = (meta.get("src"), call)
    return call


def all_calls() -> list[dict]:
    if not ROOT.exists():
        return []
    out = [c for d in ROOT.iterdir() if d.is_dir() and not d.name.startswith(".")
           if (c := _load(d))]
    out.sort(key=lambda c: c["when"] or datetime.min, reverse=True)
    return out


def title_of(call: dict) -> str:
    if call["topic"]:
        return call["topic"]
    return f"Созвон {call['when']:%d.%m}" if call["when"] else "Созвон"


# ----------------------------------------------------------------- кто есть кто

_TR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
               ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "i", "k", "l", "m", "n", "o", "p",
                "r", "s", "t", "u", "f", "h", "c", "ch", "sh", "sh", "", "i", "", "e", "iu", "ia"]))
# не имена: приставки из подписей Зума
_NOISE = {"business", "iphone", "ipad", "android", "zoom", "user", "polzovatel",
          "galaxy", "samsung", "pc", "laptop", "macbook", "vershina", "community", "official"}


def norm(label: str) -> str:
    """Подпись → латинский скелет: «Азат_Кузнецов» и «Azat Kuznetsov» совпадут."""
    s = (label or "").lower().replace("ё", "е")
    s = "".join(_TR.get(ch, ch) for ch in s)
    s = s.replace("kh", "h").replace("ts", "c").replace("zh", "z").replace("yu", "iu") \
         .replace("ya", "ia").replace("y", "i").replace("w", "v").replace("x", "ks").replace("j", "i")
    s = re.sub(r"(.)\1+", r"\1", s)
    return " ".join(t for t in re.split(r"[^a-z]+", s) if t)


def _tokens(label: str) -> list[str]:
    noise = _noise()
    return [t for t in norm(label).split() if t not in noise and len(t) > 1]


def _noise() -> set[str]:
    # служебные слова в том же виде, что и подписи после norm(): «zoom» → «zom»
    global _NOISE_N
    if _NOISE_N is None:
        _NOISE_N = {norm(w) for w in _NOISE}
    return _NOISE_N


_NOISE_N: set[str] | None = None


def _like(a: str, b: str) -> bool:
    return a == b or (min(len(a), len(b)) >= 4 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.84)


def roster_index(conn: sqlite3.Connection) -> list[dict]:
    """Кого можно узнать: участники потоков и команда (менторы, организаторы)."""
    from .roster import card_id
    out = []
    for p in conn.execute("SELECT id, name, flow, username FROM people WHERE status IN ('в потоке','выбыл')"):
        parts = _tokens(p["name"])
        out.append({"kind": "участник", "ref": card_id(p["id"]), "name": p["name"], "flow": p["flow"],
                    "last": parts[0] if parts else "", "first": parts[1] if len(parts) > 1 else "",
                    "nick": norm(p["username"] or "").replace(" ", "")})
    for u in conn.execute("SELECT id, name, login, role FROM users WHERE active=1 AND role IN "
                          "('mentor','coordinator','admin')"):
        parts = _tokens(u["name"])
        # у пользователей «Имя Фамилия»
        out.append({"kind": "команда", "ref": u["id"], "name": u["name"], "flow": None,
                    "first": parts[0] if parts else "", "last": parts[1] if len(parts) > 1 else "",
                    "nick": norm(u["login"]).replace(" ", ""), "role": u["role"]})
    return out


def _guess(label: str, index: list[dict]) -> dict | None:
    toks = _tokens(label)
    if not toks:
        return None
    glued = "".join(toks)
    # «Глеб» — это имя, а не ник alexander: по нику узнаём, только если это не чьё-то имя
    is_name = any(_like(glued, c["first"]) for c in index if c["first"])
    scored = []
    for c in index:
        first, last = c["first"], c["last"]
        if not (first or last):
            continue
        has_last = bool(last) and any(_like(t, last) for t in toks)
        has_first = bool(first) and any(_like(t, first) for t in toks)
        if has_last and has_first:
            score = 1.0
        elif first and last and (glued in (first + last, last + first) or _like(glued, first + last)
                                 or _like(glued, last + first)):
            score = 1.0
        elif c["nick"] and len(c["nick"]) >= 5 and not is_name and _like(glued, c["nick"]):
            score = 0.95
        elif c["nick"] and len(glued) >= 6 and not is_name and c["nick"].startswith(glued):
            score = 0.7          # «Ironman» → ironman_seller: похоже, но проверить
        elif has_last:
            score = 0.8
        elif has_first and len(toks) == 1:
            score = 0.6
        else:
            continue
        scored.append((score, c))
    if not scored:
        return None
    best = max(s for s, _ in scored)
    top = [c for s, c in scored if s == best]
    # одно имя на двоих («Виктория») — не гадаем, пусть отметят руками
    if len({(c["kind"], c["ref"]) for c in top}) != 1:
        return None
    return {**top[0], "sure": best >= 0.95}


def generic(label: str, index: list[dict]) -> bool:
    """Подпись, за которой на разных встречах разные люди: «Голос 3», «iPhone»,
    «Пользователь Zoom», одно имя на нескольких («Виктория»). Отметку такой
    подписи храним только для одной встречи."""
    if re.fullmatch(r"\s*(голос|voice)\s*\d+\s*|\s*без подписи\s*", (label or "").lower()):
        return True
    toks = _tokens(label)
    if not toks:
        return True
    if len(toks) == 1:
        # одно слово: общее, только если это имя нескольких людей («Виктория»);
        # ни с кем не совпало («Ironman») — уникальный ник, не общая подпись
        t = toks[0]
        same = {(c["kind"], c["ref"]) for c in index
                if (c["first"] and _like(t, c["first"])) or (c["last"] and _like(t, c["last"]))}
        return len(same) > 1
    return False


def resolve(conn: sqlite3.Connection, labels: list[str], index: list[dict] | None = None,
            call_id: str = "") -> dict[str, dict]:
    """Подпись → {kind, ref, name, flow, how, generic}. how: вручную | узнан | догадка | —.
    Ручная отметка этой встречи важнее общей."""
    index = index if index is not None else roster_index(conn)
    by_ref = {(c["kind"], c["ref"]): c for c in index}
    manual = {(r["alias"], r["call_id"]): r for r in conn.execute(
        "SELECT * FROM call_aliases WHERE call_id IN ('', ?)", (call_id,))}
    out = {}
    for label in labels:
        gen = generic(label, index)
        m = manual.get((norm(label), call_id)) or (None if gen else manual.get((norm(label), "")))
        if m:
            c = by_ref.get((m["kind"], m["ref"]))
            out[label] = {"kind": m["kind"], "ref": m["ref"], "name": c["name"] if c else label,
                          "flow": c["flow"] if c else None, "how": "вручную", "generic": gen,
                          "scope": "встреча" if m["call_id"] else "везде"}
            continue
        g = None if re.fullmatch(r"(голос|voice)\s*\d+|без подписи", label.strip().lower()) else _guess(label, index)
        out[label] = ({"kind": g["kind"], "ref": g["ref"], "name": g["name"], "flow": g["flow"],
                       "how": "узнан" if g["sure"] else "догадка", "generic": gen} if g
                      else {"kind": None, "ref": None, "name": label, "flow": None, "how": "—", "generic": gen})
    return out


def set_alias(conn: sqlite3.Connection, label: str, kind: str, ref: int | None, who: str,
              call_id: str = "") -> None:
    """call_id '' — отметка для всех встреч, иначе только для этой."""
    if kind == "":
        conn.execute("DELETE FROM call_aliases WHERE alias=? AND call_id=?", (norm(label), call_id))
        return
    if kind not in ("участник", "команда", "чужой"):
        raise ValueError("kind")
    conn.execute("INSERT OR REPLACE INTO call_aliases (alias, call_id, label, kind, ref, set_by, set_at) "
                 "VALUES (?,?,?,?,?,?,?)",
                 (norm(label), call_id, label, kind, ref if kind != "чужой" else None, who,
                  datetime.now().isoformat(timespec="seconds")))


# ----------------------------------------------------------------- для страниц

def enrich(conn: sqlite3.Connection, call: dict, index: list[dict] | None = None) -> dict:
    """Встреча + кто есть кто + к какому потоку относится."""
    who = resolve(conn, [s["who"] for s in call["talk"]], index, call["id"])
    people = []
    for s in call["talk"]:
        r = who[s["who"]]
        people.append({**s, **{k: r[k] for k in ("kind", "ref", "flow", "how", "generic")},
                       "scope": r.get("scope"), "label": s["who"],
                       "name": r["name"]})
    flow, how = _flow_of(conn, call, people)
    return {**call, "people": people, "flow": flow, "flow_how": how,
            "cards": {p["ref"] for p in people if p["kind"] == "участник"},
            "staff": {p["ref"] for p in people if p["kind"] == "команда"},
            "n_members": sum(1 for p in people if p["kind"] == "участник"),
            "n_unknown": sum(1 for p in people if not p["kind"])}


def _flow_of(conn: sqlite3.Connection, call: dict, people: list[dict]) -> tuple[str | None, str | None]:
    """К какому потоку встреча: отмечено вручную → по участникам → по менторам
    → по дате (если тогда шёл ровно один поток). how — как определили."""
    m = conn.execute("SELECT flow FROM call_flow WHERE call_id=?", (call["id"],)).fetchone()
    if m:
        return m["flow"], "вручную"
    votes: dict[str, int] = {}
    for p in people:
        if p["kind"] == "участник" and p["flow"]:
            votes[p["flow"]] = votes.get(p["flow"], 0) + 1
    if votes:
        return max(votes, key=votes.get), "по участникам"
    staff = [p["ref"] for p in people if p["kind"] == "команда"]
    if staff:
        marks = ",".join("?" * len(staff))
        for r in conn.execute(
                f"SELECT f.name, COUNT(*) n FROM flow_staff s JOIN flow_teams t ON t.id=s.flow_team_id "
                f"JOIN flows f ON f.id=t.flow_id WHERE s.user_id IN ({marks}) "
                f"GROUP BY f.name ORDER BY n DESC LIMIT 1", staff):
            return r["name"], "по менторам"
    if call["when"]:
        day = call["when"].date().isoformat()
        running = [f["name"] for f in conn.execute("SELECT * FROM flows WHERE status<>'готовится'")
                   if (f["start"] or "0000") <= day <= (f["finish"] or (f["finished_at"] or "9999")[:10])]
        if len(running) == 1:
            return running[0], "по дате"
    return None, None


def set_flow(conn: sqlite3.Connection, call_id: str, flow: str | None, who: str) -> None:
    """flow: имя потока, "" — снять ручную отметку, "-" — встреча вне потоков."""
    if flow == "":
        conn.execute("DELETE FROM call_flow WHERE call_id=?", (call_id,))
        return
    conn.execute("INSERT OR REPLACE INTO call_flow VALUES (?,?,?,?)",
                 (call_id, None if flow == "-" else flow, who, datetime.now().isoformat(timespec="seconds")))


def visible(call: dict, user: sqlite3.Row, ids: list[int] | None) -> bool:
    """Организаторы видят все встречи «Вершины»; ментор — где были его участники или он сам."""
    if ids is None:
        return True
    return bool(call["cards"] & set(ids)) or user["id"] in call["staff"]


def listing(conn: sqlite3.Connection, user: sqlite3.Row, ids: list[int] | None) -> list[dict]:
    index = roster_index(conn)
    return [c for c in (enrich(conn, c, index) for c in all_calls()) if visible(c, user, ids)]


def docx_path(call_id: str) -> Path | None:
    if not re.fullmatch(r"[\w-]{6,40}", call_id or ""):
        return None
    p = ROOT / call_id / "meeting.docx"
    return p if p.is_file() else None


def get(conn: sqlite3.Connection, call_id: str) -> dict | None:
    if not re.fullmatch(r"[\w-]{6,40}", call_id or ""):
        return None
    folder = ROOT / call_id
    call = _load(folder) if folder.is_dir() else None
    return enrich(conn, call) if call else None


def of_person(conn: sqlite3.Connection, card: int) -> list[dict]:
    """Встречи, где был участник, и сколько он говорил — для его карточки."""
    index = roster_index(conn)
    out = []
    for c in all_calls():
        e = enrich(conn, c, index)
        mine = [p for p in e["people"] if p["kind"] == "участник" and p["ref"] == card]
        if mine:
            out.append({"id": e["id"], "title": title_of(e), "when": e["when"], "secs": e["secs"],
                        "talk": sum(p["secs"] for p in mine), "turns": sum(p["turns"] for p in mine),
                        "has_summary": bool(e["summary"])})
    return out
