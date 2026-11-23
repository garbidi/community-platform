#!/usr/bin/env python3
"""Собирает рабочий каталог помощника под конкретную машину.

Зачем отдельный шаг. Права Claude Code в .claude/settings.json задаются
абсолютными путями, и если путь не совпадёт с реальным, разрешение просто
не сработает — без ошибки и без единой строчки в логе. Тот же молчаливый
отказ даёт недоверенный каталог: без hasTrustDialogAccepted в .claude.json
файл прав игнорируется целиком. Поэтому и то и другое собирается из
шаблонов по фактической раскладке, а не правится руками.

Запуск (все значения по умолчанию — под текущую машину):

    python3 deploy/render_config.py --owner "Глеб"

Существующие CLAUDE.md и settings.json не трогаются: чтобы перезаписать,
нужен --force. memory.md создаётся пустым и никогда не перезаписывается —
это память помощника, её терять нельзя.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"


def values(args: argparse.Namespace) -> dict[str, str]:
    """Раскладка, под которую собираем. Порядок ключей неважен."""
    return {
        "JARVIS_DIR": args.jarvis,
        "APP_DIR": args.app,
        "KB_DIR": args.kb,
        "BOT_PY": args.bot_python,
        "KB_PY": args.kb_python,
        "CLAUDE_HOME": args.claude_home,
        "OWNER": args.owner,
    }


def render(template: str, vals: dict[str, str]) -> str:
    text = TEMPLATES.joinpath(template).read_text(encoding="utf-8")
    for key, value in vals.items():
        text = text.replace("{{" + key + "}}", value)
    left = [line for line in text.splitlines() if "{{" in line]
    if left:
        raise SystemExit("В шаблоне остались незаполненные места:\n  "
                         + "\n  ".join(left[:5]))
    return text


def put(path: Path, text: str, force: bool) -> str:
    """Пишет файл. Возвращает, что именно сделал — для отчёта."""
    if path.exists() and not force:
        return f"оставлен как был: {path}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return f"записан: {path}"


def trust(claude_home: Path, jarvis: Path) -> str:
    """Помечает рабочий каталог доверенным.

    Без этого Claude Code молча игнорирует .claude/settings.json целиком,
    и помощник остаётся без прав, ничего об этом не сообщая.
    """
    cfg = claude_home / ".claude.json"
    data = {}
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return (f"ОСТОРОЖНО: {cfg} не разбирается как JSON, "
                    "доверие каталогу не выставлено — проверьте вручную")
    projects = data.setdefault("projects", {})
    entry = projects.setdefault(str(jarvis), {})
    if entry.get("hasTrustDialogAccepted") is True:
        return f"каталог уже доверенный: {jarvis}"
    entry["hasTrustDialogAccepted"] = True
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    return f"каталог помечен доверенным: {jarvis}"


def main() -> int:
    p = argparse.ArgumentParser(
        description="Собрать рабочий каталог помощника под эту машину.")
    p.add_argument("--owner", default=os.environ.get("VERSHINA_OWNER", "хозяин"),
                   help="имя хозяина в инструкциях, именительный падеж")
    p.add_argument("--jarvis", default=os.environ.get(
        "VERSHINA_JARVIS_DIR", "/opt/vershina/vershina-jarvis"))
    p.add_argument("--app", default=os.environ.get(
        "VERSHINA_APP_DIR", "/opt/vershina/src/vershina-app"))
    p.add_argument("--kb", default=os.environ.get(
        "VERSHINA_KB_SRC", "/opt/vershina/src/vershina-kb"))
    p.add_argument("--bot-python", default=os.environ.get(
        "VERSHINA_BOT_PYTHON", "/opt/vershina/venvs/vershina-bot/bin/python"))
    p.add_argument("--kb-python", default=os.environ.get(
        "VERSHINA_KB_PYTHON", "/opt/vershina/venvs/vershina-kb/bin/python"))
    p.add_argument("--claude-home", default=os.environ.get(
        "CLAUDE_HOME", str(Path.home())),
        help="домашний каталог, в котором лежат .claude и .claude.json")
    p.add_argument("--force", action="store_true",
                   help="перезаписать CLAUDE.md и settings.json, если они есть")
    p.add_argument("--dry-run", action="store_true",
                   help="показать, что получится, и ничего не писать")
    args = p.parse_args()

    vals = values(args)
    jarvis = Path(args.jarvis)
    claude_home = Path(args.claude_home)

    claude_md = render("CLAUDE.md.tmpl", vals)
    settings = render("settings.json.tmpl", vals)

    # Права — это JSON. Если шаблон подставкой испортили, лучше узнать здесь,
    # чем получить молча обесправленного помощника.
    try:
        json.loads(settings)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"settings.json после подстановки сломан: {exc}")

    if args.dry_run:
        print("— проверка, ничего не записано —")
        print(f"рабочий каталог: {jarvis}")
        print(f"хозяин: {args.owner}")
        print(f"CLAUDE.md: {len(claude_md)} символов")
        print(f"settings.json: разрешений "
              f"{len(json.loads(settings)['permissions']['allow'])}, "
              f"запретов {len(json.loads(settings)['permissions']['deny'])}")
        return 0

    report = [
        put(jarvis / "CLAUDE.md", claude_md, args.force),
        put(jarvis / ".claude" / "settings.json", settings, args.force),
        # Память всегда стартует пустой и никогда не перезаписывается.
        put(jarvis / "memory.md", "", force=False),
        trust(claude_home, jarvis),
    ]
    print("\n".join(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
