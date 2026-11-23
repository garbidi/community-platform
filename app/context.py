"""Что уже известно об участнике — короткой справкой для Claude.

Разбор и созвон Claude читает не с нуля: анкета, подтверждённые сведения,
декларация с отметками шагов, прошлые разборы и заметки со созвонов. Так он
сверяет новое с известным, видит расхождения и не выдаёт старое за открытие.
Справка короткая (до ~2 тыс. знаков на человека): на созвоне их шесть-восемь
в одном запросе, и платить за длинные было бы дорого.
"""

from __future__ import annotations

import re
import sqlite3

LIMIT = 3000


def _cut(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def _section(md: str, title: str) -> str:
    """Раздел «## title» из выжимки разбора."""
    m = re.search(rf"^##\s*{re.escape(title)}\s*$(.*?)(?=^##\s|\Z)", md or "", flags=re.M | re.S)
    return m.group(1).strip() if m else ""


def person(conn: sqlite3.Connection, card: int, skip_review: int | None = None,
           skip_call: str | None = None) -> str:
    """Справка по карточке (card < 0 — участник потока на платформе)."""
    from . import person_info
    lines = []
    p = conn.execute("SELECT * FROM people WHERE id=?", (-card,)).fetchone() if card < 0 else None
    if p:
        team = conn.execute("SELECT title FROM teams WHERE id=?", (p["team_id"],)).fetchone() if p["team_id"] else None
        lines.append(f"{p['name']} — поток {p['flow']}" + (f", {team['title'].split('—')[0].strip()}" if team else "")
                     + (" (на сегодня — выбыл; запись может быть старше)" if p["status"] == "выбыл" or p["left_at"] else ""))
        try:
            from .fourday import profile
            prof = "; ".join(f"{k}: {_cut(v, 120)}" for k, v in profile(p))
            if prof:
                lines.append("Анкета: " + _cut(prof, 500))
        except Exception:
            pass
    facts = person_info.facts(conn, card, money=True)
    known = [f"{f['title']}: {_cut(f['current']['value'], 160)}" for f in facts.values() if f["current"]]
    if known:
        lines.append("Сведения: " + "; ".join(known))
    rev = conn.execute("SELECT id, held_on, summary FROM reviews WHERE tg_user_id=? AND status='готов' AND summary IS NOT NULL"
                       + (" AND id<>?" if skip_review else "") + " ORDER BY id DESC LIMIT 1",
                       (card, skip_review) if skip_review else (card,)).fetchone()
    if rev:
        short = _section(rev["summary"], "Кратко о человеке") or _section(rev["summary"], "Что выкопали")
        if short:
            lines.append(f"Из разбора {rev['held_on'] or ''}: {_cut(short, 600)}")
    if _table(conn, "call_notes"):
        import json
        notes = conn.execute("SELECT * FROM call_notes WHERE card=? AND call_id<>? "
                             "ORDER BY COALESCE(call_when, created_at) DESC LIMIT 2", (card, skip_call or "")).fetchall()
        for i, n in enumerate(notes):
            when = (n["call_when"] or "")[:10]
            if n["note"]:
                lines.append(f"Созвон {when} «{_cut(n['call_title'] or '', 40)}»: {_cut(n['note'], 260)}")
            if i == 0:
                try:
                    promised = json.loads(n["promises"] or "[]")
                except ValueError:
                    promised = []
                if promised:
                    lines.append(f"Обещал на созвоне {when}: " + "; ".join(_cut(x, 120) for x in promised[:5]))
    if p:
        goals = conn.execute("SELECT * FROM decl_goals WHERE person_id=? ORDER BY no", (p["id"],)).fetchall()
        for g in goals[:8]:
            steps = conn.execute("SELECT title, done FROM decl_steps WHERE goal_id=? ORDER BY id", (g["id"],)).fetchall()
            st = ", ".join(("✓ " if s["done"] else "") + _cut(s["title"], 50) for s in steps[:5])
            lines.append(f"Цель: {_cut(g['title'], 140)}" + (f" [{_pct(g['progress'])}%]" if g["progress"] else "")
                         + (f" — шаги: {st}" if st else ""))
        if p["money_goal"]:
            lines.append(f"Цель в деньгах: {p['money_goal']}" + (f", факт {p['money_fact']}" if p["money_fact"] else ""))
    out = "\n".join(lines)
    return out if len(out) <= LIMIT else out[:LIMIT - 1] + "…"


def _table(conn, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())



def _pct(v) -> int:
    # из таблиц прогресс приходит долей (0,75), на платформе — процентами
    return round(v * 100) if v <= 1 else round(v)
