#!/usr/bin/env python3
"""Помощник из командной строки — им удобно проверять ответы без телеграма.

    ask.py "почему у Петрова 0 вовлечений"
    ask.py --engine rules "кто затих"
    ask.py                      # диалог, выход — пустая строка
"""

from __future__ import annotations

import argparse
import sys

from app import brain


def once(question: str, engine: str | None, trace: bool) -> None:
    res = brain.answer(question, engine=engine)
    print(res["text"])
    if trace:
        t = res["tokens"]
        print(f"\n— движок {res['engine']}, инструменты: "
              f"{', '.join(res['tools']) or 'не звал'}"
              + (f", токенов {t['in']}→{t['out']}" if t["in"] else ""),
              file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description="помощник по сообществу")
    ap.add_argument("question", nargs="*", help="вопрос; без него — диалог")
    ap.add_argument("--engine", choices=["api", "rules"], default=None,
                    help="по умолчанию api, если задан ANTHROPIC_API_KEY")
    ap.add_argument("--trace", action="store_true", help="показать инструменты и токены")
    args = ap.parse_args()

    if args.question:
        once(" ".join(args.question), args.engine, args.trace)
        return

    print(f"движок: {args.engine or brain.engine_name()}. Пустая строка — выход.")
    history: list[dict] = []
    while True:
        try:
            q = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not q:
            return
        res = brain.answer(q, history=history, engine=args.engine)
        print("\n" + res["text"])
        if args.trace:
            print(f"\n— инструменты: {', '.join(res['tools']) or 'нет'}", file=sys.stderr)
        history += [{"role": "user", "content": q},
                    {"role": "assistant", "content": res["text"]}]
        history[:] = history[-8:]


if __name__ == "__main__":
    main()
