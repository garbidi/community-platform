#!/usr/bin/env python3
"""Второй слой разметки: смысл реплики ставит модель, а не правила.

Правила видят форму — длину, цифры, вопросительный знак, отклик. Смысл
(«объясняет», «берёт обязательство», «рассказывает о себе») формой не ловится:
в корпусе «Вершины» такие реплики лежат вперемешку с флудом. Поэтому метка
ставится моделью один раз на реплику и хранится в базе — дальше ей пользуются
и отбор материала, и поиск.

    python3 marks.py plan                сколько не размечено и сколько это стоит
    python3 marks.py export пачка.jsonl  выгрузить пачку на разметку (+ инструкция)
    python3 marks.py import ответ.jsonl  принять размеченное
    python3 marks.py stats               что уже размечено

Формат ответа — по строке JSON на реплику:
    {"id": "3000000001:1234", "label": "знание", "usefulness": 3}
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import corpus, signal, store  # noqa: E402

LABELS = ("знание", "вопрос", "обязательство", "результат", "личное",
          "поддержка", "организация", "спор", "болтовня")

INSTRUCTION = f"""Размечай реплики из рабочих чатов бизнес-сообщества «Вершина».
Каждая строка входа — JSON: id, автор, дата, вид сообщения, текст.

Для каждой реплики верни строку JSON:
{{"id": "<как на входе>", "label": "<одна метка>", "usefulness": <0-3>}}

Метки: {", ".join(LABELS)}.
  знание — объясняет, учит, делится опытом или разбором
  вопрос — спрашивает по делу
  обязательство — берёт на себя, называет срок
  результат — сообщает свой факт или цифру
  личное — про себя: состояние, семья, сомнения, усталость
  поддержка — подбадривает, хвалит, благодарит
  организация — время, ссылки, правила, объявления
  спор — возражает, не соглашается, конфликтует
  болтовня — шутки, флуд, междометия, реакции без содержания

usefulness — насколько реплика помогает понять человека и найти к нему подход:
  3 — многое говорит о нём: как думает, чего хочет, где буксует
  2 — полезна как штрих
  1 — почти ничего не добавляет
  0 — служебное, в разбор не нужно

Только строки JSON, по одной на реплику, без пояснений."""


def _all_replies() -> list[dict]:
    out = []
    for p in corpus.participants():
        out.extend(signal.replies_of(p["user_id"]))
    return out


def _marked(conn) -> set[tuple[int, int]]:
    return {(r["chat_id"], r["msg_id"]) for r in
            conn.execute("SELECT chat_id, msg_id FROM reply_marks")}


def cmd_plan() -> int:
    conn = store.connect()
    reps = _all_replies()
    done = _marked(conn)
    todo = [r for r in reps if (r["chat_id"], r["msg_id"]) not in done]
    chars = sum(min(len(r["text"]), 600) + 90 for r in todo)
    print(f"реплик всего: {len(reps):,}, размечено: {len(done):,}, осталось: {len(todo):,}")
    print(f"объём на разметку: {chars:,} знаков (текст режется до 600 знаков на реплику)")
    for lo, hi in ((2, "пессимистично"), (3, "оптимистично")):
        tok_in = chars / lo / 1e6
        tok_out = len(todo) * 25 / 1e6        # ~25 токенов на строку ответа
        print(f"  {hi} ({lo} знака/токен): вход {tok_in:.2f} млн, выход {tok_out:.2f} млн →"
              f" Haiku ≈ ${tok_in*1 + tok_out*5:.2f}"
              f" · Sonnet ≈ ${tok_in*2 + tok_out*10:.2f}"
              f" · Opus ≈ ${tok_in*5 + tok_out*25:.2f}")
    print("Через Batch API вдвое дешевле. Разметка разовая: дальше только новые реплики.")
    return 0


def cmd_export(path: str, limit: int) -> int:
    conn = store.connect()
    done = _marked(conn)
    todo = [r for r in _all_replies() if (r["chat_id"], r["msg_id"]) not in done]
    todo.sort(key=lambda r: -r["score"])
    batch = todo[:limit]
    out = Path(path)
    with out.open("w", encoding="utf-8") as f:
        for r in batch:
            kind = ("кружок" if "video_note" in r["kinds"]
                    else "голосовое" if "voice" in r["kinds"] else "текст")
            f.write(json.dumps({
                "id": f"{r['chat_id']}:{r['msg_id']}",
                "автор": corpus.name_of(r["user_id"]),
                "дата": r["when"].strftime("%d.%m.%Y %H:%M"),
                "вид": kind,
                "текст": r["text"][:600],
            }, ensure_ascii=False) + "\n")
    Path(str(out) + ".инструкция.txt").write_text(INSTRUCTION, encoding="utf-8")
    print(f"выгружено {len(batch)} реплик → {out}")
    print(f"инструкция → {out}.инструкция.txt")
    print(f"осталось после этой пачки: {len(todo) - len(batch):,}")
    return 0


def cmd_import(path: str, model: str) -> int:
    conn = store.connect()
    ok = bad = 0
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip().strip(",")
        if not line or line in ("[", "]"):
            continue
        try:
            item = json.loads(line)
            chat_id, msg_id = item["id"].split(":")
            label = item["label"].strip().lower()
            if label not in LABELS:
                raise ValueError(f"неизвестная метка {label}")
            rows.append((int(chat_id), int(msg_id), label,
                         max(0, min(3, int(item["usefulness"]))),
                         item.get("note"), model))
            ok += 1
        except Exception as exc:
            bad += 1
            print(f"  пропущено: {exc}")
    conn.executemany(
        "INSERT INTO reply_marks (chat_id, msg_id, label, usefulness, note, model) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(chat_id, msg_id) DO UPDATE SET "
        "label=excluded.label, usefulness=excluded.usefulness, model=excluded.model",
        rows)
    conn.commit()
    print(f"принято меток: {ok}" + (f", ошибок: {bad}" if bad else ""))
    return 0


def cmd_stats() -> int:
    conn = store.connect()
    total = conn.execute("SELECT COUNT(*) FROM reply_marks").fetchone()[0]
    if not total:
        print("Размеченных реплик пока нет. Начните с: python3 marks.py export пачка.jsonl")
        return 0
    print(f"размечено реплик: {total:,}\n")
    print("  метка            реплик   средняя польза")
    for r in conn.execute(
            "SELECT label, COUNT(*) n, ROUND(AVG(usefulness),2) u FROM reply_marks "
            "GROUP BY label ORDER BY n DESC"):
        print(f"  {r['label']:16} {r['n']:6}   {r['u']}")
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd, *rest = argv
    if cmd == "plan":
        return cmd_plan()
    if cmd == "export":
        return cmd_export(rest[0] if rest else "пачка.jsonl",
                          int(rest[1]) if len(rest) > 1 else 200)
    if cmd == "import":
        return cmd_import(rest[0], rest[1] if len(rest) > 1 else "claude-code")
    if cmd == "stats":
        return cmd_stats()
    print(f"Не знаю команды «{cmd}».")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
