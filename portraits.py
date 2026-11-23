#!/usr/bin/env python3
"""Конвейер портретов: кому нужен, выжимка на одного, сохранение.

Портрет пишет модель, и писать его по полному материалу на каждого из пятидесяти
человек — это лишние сотни тысяч токенов. Поэтому здесь выжимка: показатели
участия и самые содержательные реплики, урезанные по длине. Для портрета важен
характер участия, а не дословность.

    python3 portraits.py plan            кому нужен портрет
    python3 portraits.py next [N]        завести запросы и напечатать выжимки
    python3 portraits.py save <tg> файл  сохранить портрет участника
    python3 portraits.py done            что уже собрано
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import analysis, corpus, signal, store  # noqa: E402

REPLIES = 32        # сколько реплик кладём в выжимку
TRIM = 200          # и до какой длины режем каждую


def _needy(conn) -> list[dict]:
    """У кого материала достаточно, а готового портрета ещё нет."""
    out = []
    for p in corpus.participants():
        tg = p["user_id"]
        if analysis.readiness(tg)["level"] != "достаточно":
            continue
        row = analysis.latest(conn, tg, "участнику")
        if row and row["status"] == "готов":
            continue
        out.append(p)
    out.sort(key=lambda p: -p["messages"])
    return out


def cmd_plan() -> int:
    conn = store.connect()
    need = _needy(conn)
    done = conn.execute(
        "SELECT COUNT(*) FROM analyses WHERE kind='участнику' AND status='готов'"
    ).fetchone()[0]
    print(f"портретов собрано: {done}, ждут: {len(need)}")
    for p in need[:12]:
        print(f"  {p['user_id']:>12}  {p['name']}")
    if len(need) > 12:
        print(f"  … и ещё {len(need) - 12}")
    return 0


def digest(tg: int) -> str:
    """Выжимка по одному человеку: показатели плюс отобранные реплики."""
    prof = corpus.profile_of(tg)
    reps = signal.replies_of(tg)
    best = sorted(reps, key=lambda r: -r["score"])[:REPLIES]
    best.sort(key=lambda r: r["date"])

    lines = [
        f"### {prof['name']} · tg {tg}",
        f"участие: {prof['first_seen']} — {prof['last_seen']}, состояние {prof['status']}"
        + (f", молчит {prof['silent_days']} дн." if prof["silent_days"] else ""),
        f"сообщений {prof['messages']}, слов {prof['words']}, в среднем {prof['avg_words']} на сообщение; "
        f"голосом {prof['circles'] + prof['voice']} ({prof['audio_min']} мин, темп "
        f"{prof['speech_rate'] or '—'} слов/мин)",
        f"отвечает другим {prof['reply_share']}, задержка ответа {prof['reply_lag_min'] or '—'} мин, "
        f"вопросов {prof['question_share']}, пик {prof.get('peak_hour_label') or '—'} МСК, "
        f"ночных {prof['night_share']}",
        f"за 14 дней {prof['last14']} против {prof['prev14']} до этого; "
        f"молчал максимум {prof['max_silence_days']} дн., активных месяцев {prof['active_months']}",
        "реплики:",
    ]
    for r in best:
        kind = ("кружок" if "video_note" in r["kinds"]
                else "голос" if "voice" in r["kinds"] else "текст")
        text = " ".join(r["text"].split())
        if len(text) > TRIM:
            text = text[:TRIM] + "…"
        lines.append(f"- [{r['when'].strftime('%d.%m')} {kind}] {text}")
    return "\n".join(lines)


def cmd_next(count: int) -> int:
    conn = store.connect()
    need = _needy(conn)[:count]
    if not need:
        print("портреты собраны всем, у кого достаточно данных.")
        return 0
    admin = store.user_by_login(conn, "admin")
    for p in need:
        tg = p["user_id"]
        row = analysis.latest(conn, tg, "участнику")
        if not (row and row["status"] == "ожидает"):
            analysis.request(conn, tg, admin, kind="участнику")
        print(digest(tg))
        print()
    conn.commit()
    print(f"— выжимок напечатано: {len(need)}; осталось после них: "
          f"{len(_needy(conn)) - len(need)}")
    return 0


def cmd_save(tg: int, path: str) -> int:
    conn = store.connect()
    row = analysis.latest(conn, tg, "участнику")
    if row is None:
        admin = store.user_by_login(conn, "admin")
        analysis.request(conn, tg, admin, kind="участнику")
        row = analysis.latest(conn, tg, "участнику")
    text = Path(path).read_text(encoding="utf-8")
    analysis.save(conn, row["id"], text)
    conn.commit()
    print(f"портрет сохранён: {corpus.name_of(tg)} ({len(text)} знаков)")
    return 0


def cmd_done() -> int:
    conn = store.connect()
    rows = conn.execute(
        "SELECT tg_user_id, finished_at, LENGTH(text) n FROM analyses "
        "WHERE kind='участнику' AND status='готов' ORDER BY id").fetchall()
    print(f"собрано портретов: {len(rows)}")
    for r in rows:
        print(f"  {corpus.name_of(r['tg_user_id']):28} {r['n']:>5} знаков  {r['finished_at']}")
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, *rest = argv
    if cmd == "plan":
        return cmd_plan()
    if cmd == "next":
        return cmd_next(int(rest[0]) if rest else 3)
    if cmd == "save":
        return cmd_save(int(rest[0]), rest[1])
    if cmd == "done":
        return cmd_done()
    print(f"не знаю команды «{cmd}»")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
