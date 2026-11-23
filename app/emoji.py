"""Премиум-эмодзи сообщества — как в сообщениях Марьи.

Организаторы пишут посты фирменными буквами (набор «Cyrillic Font»: «ММ7»,
«ЛИДЕРБОРД», «ПО ВОВЛЕЧЕНИЮ») и своими версиями значков 📌 ✔️ ❤️ и цифр.
В телеграме такой эмодзи — обычный символ плюс номер картинки; без номера
он показывается простым эмодзи. Платформа помнит номера и собирает пост так же.

Как пишется текст поста (и как попросим писать ИИ):
- [[ЛИДЕРБОРД]] — слово фирменными буквами (буквы, цифры, знаки);
- 📌 ✔️ ❤️ 3️⃣ … — обычные эмодзи; если у сообщества есть своя премиум-версия,
  она подставится сама.
Отправить такое может только аккаунт с Telegram Premium (у Марьи он есть).
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

DIR = Path("/data/emoji")
LETTER_SET = 1860749763008266254          # «Cyrillic Font», им набраны заголовки
ABC = "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
# знаки набора по порядку картинок (сверено по картинкам 14.10.2026)
EXTRA = {33: "Ђ", 34: "Ґ", 35: "Є", 36: "І", 37: "Ї", 38: "Ў", 39: "0", 49: "!", 50: "?", 51: "−",
         52: "+", 53: "÷", 54: "×", 55: "=", 56: "≠", 57: "≈", 58: ">", 59: "<", 60: "%", 61: "№",
         62: "₽", 65: "*", 66: "(", 67: ")", 68: "«", 69: "»", 70: ":", 71: ";", 72: ".", 73: ",", 74: "…"}
KEYCAP = {str(d): f"{d}️⃣" for d in range(10)}
# второй шрифт — «TD-color», тёмно-зелёные буквы (её «ДЕНЬ»). Буквы в наборе вперемешку
# и подписаны цифрами-кнопками, поэтому соответствие — по картинкам (сверено 14.10.2026)
TD_SET = 4048052911790358526
TD_LETTERS = {7: "А", 8: "Б", 9: "Г", 10: "Е", 11: "Ж", 12: "З", 13: "И", 14: "Л", 15: "М", 16: "С",
              17: "Ф", 18: "Х", 19: "Ц", 20: "Ч", 21: "Ш", 22: "Щ", 23: "Ъ", 24: "Ы", 25: "Ь", 26: "Э",
              27: "Ю", 28: "Я", 29: "Д", 30: "Й", 31: "В", 32: "К", 33: "Н", 34: "О", 35: "П", 36: "Р",
              37: "Т", 38: "У"}
FONTS = {LETTER_SET: "основной", TD_SET: "зелёный"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS tg_emoji (
    doc_id  INTEGER PRIMARY KEY,
    alt     TEXT,                 -- символ-заменитель (🔤, 📌, 3️⃣…)
    set_id  INTEGER,
    letter  TEXT,                 -- для фирменных букв: какая это буква/цифра/знак
    font    TEXT,                 -- шрифт букв: основной | зелёный
    uses    INTEGER NOT NULL DEFAULT 0   -- сколько раз встречается у организаторов
);
"""


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    if "font" not in {r[1] for r in conn.execute("PRAGMA table_info(tg_emoji)")}:
        conn.execute("ALTER TABLE tg_emoji ADD COLUMN font TEXT")


# ----------------------------------------------------------------- словарь

def load_letter_set(conn: sqlite3.Connection) -> int:
    """Фирменные шрифты — из скачанных наборов (sets.json, порядок картинок)."""
    try:
        sets = json.loads((DIR / "sets.json").read_text())["sets"]
    except (OSError, ValueError, KeyError):
        return 0
    n = 0
    for sid, font in FONTS.items():
        for it in (sets.get(str(sid)) or {}).get("items") or []:
            i = it["i"]
            if sid == LETTER_SET:
                ch = ABC[i] if i < 33 else (str(i - 39) if 40 <= i <= 48 else EXTRA.get(i))
            else:
                ch = TD_LETTERS.get(i)
            conn.execute("INSERT INTO tg_emoji (doc_id, alt, set_id, letter, font) VALUES (?,?,?,?,?) "
                         "ON CONFLICT(doc_id) DO UPDATE SET alt=excluded.alt, set_id=excluded.set_id, "
                         "letter=excluded.letter, font=excluded.font",
                         (it["id"], it["alt"], sid, ch, font if ch else None))
            n += 1
    return n


def learn(conn: sqlite3.Connection, senders: list[str]) -> dict:
    """Какими премиум-эмодзи пишут организаторы (по их сообщениям в чатах):
    сохраняем номера, подписи и картинки для предпросмотра."""
    from . import tg_account
    marks = ",".join("?" * len(senders))
    used: Counter = Counter()
    for r in conn.execute(f"SELECT emojis FROM chat_messages WHERE emojis IS NOT NULL AND username IN ({marks})",
                          senders):
        for _, _, doc in json.loads(r["emojis"]):
            used[int(doc)] += 1
    DIR.mkdir(parents=True, exist_ok=True)
    known = {r["doc_id"] for r in conn.execute("SELECT doc_id FROM tg_emoji WHERE alt IS NOT NULL AND alt<>''")}
    need = [d for d in used if d not in known or not (DIR / f"{d}.thumb").exists()]
    if need:
        res = tg_account.emoji_docs(need, DIR)
        for d, info in (res.get("docs") or {}).items():
            conn.execute("INSERT INTO tg_emoji (doc_id, alt, set_id) VALUES (?,?,?) "
                         "ON CONFLICT(doc_id) DO UPDATE SET alt=excluded.alt, set_id=excluded.set_id",
                         (int(d), info["alt"], info["set"]))
    for d, n in used.items():
        conn.execute("INSERT INTO tg_emoji (doc_id, uses) VALUES (?,?) "
                     "ON CONFLICT(doc_id) DO UPDATE SET uses=excluded.uses", (d, n))
    return {"emoji": len(used), "new": len(need)}


_STYLE: dict[str, int] | None = None
_LETTERS: dict[str, int] | None = None


def _maps() -> tuple[dict[str, int], dict[str, dict[str, int]]]:
    """символ → премиум-версия организаторов (самая частая; буквы шрифтов не в счёт —
    у «зелёного» они подписаны цифрами); шрифт → {буква → картинка}."""
    global _STYLE, _LETTERS
    if _STYLE is None:
        from . import store
        conn = store.connect()
        try:
            style: dict[str, tuple[int, int]] = {}
            for r in conn.execute("SELECT doc_id, alt, uses FROM tg_emoji WHERE alt IS NOT NULL AND alt<>'' "
                                  "AND alt<>'🔤' AND uses>0 AND letter IS NULL"):
                if r["alt"] not in style or r["uses"] > style[r["alt"]][1]:
                    style[r["alt"]] = (r["doc_id"], r["uses"])
            _STYLE = {k: v[0] for k, v in style.items()}
            letters: dict[str, dict[str, int]] = {}
            for r in conn.execute("SELECT doc_id, letter, font FROM tg_emoji WHERE letter IS NOT NULL "
                                  "ORDER BY uses"):
                letters.setdefault(r["font"], {})[r["letter"]] = r["doc_id"]   # частые — последними, побеждают
            _LETTERS = letters
        finally:
            conn.close()
    return _STYLE, _LETTERS


def reset() -> None:
    global _STYLE, _LETTERS
    _STYLE = _LETTERS = None


# ----------------------------------------------------------------- сборка

def _u16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


_WORD = re.compile(r"\[\[([^\[\]\n|]{1,60})(?:\|([^\[\]\n]{1,20}))?\]\]")


def build(text: str) -> tuple[str, list[tuple[int, int, int]]]:
    """Текст с [[СЛОВОМ]] → (текст для телеграма, [(смещение, длина, номер картинки)])."""
    style, letters = _maps()
    out, ents, pos = [], [], 0

    def put(chunk: str, doc: int | None = None):
        nonlocal pos
        if doc:
            ents.append((pos, _u16(chunk), doc))
        out.append(chunk)
        pos += _u16(chunk)

    def plain(seg: str):
        # обычный текст: значки, для которых у организаторов есть премиум-версия
        i = 0
        alts = sorted(style, key=len, reverse=True)
        while i < len(seg):
            for a in alts:
                if seg.startswith(a, i):
                    put(a, style[a]); i += len(a); break
                if a.endswith("️") and seg.startswith(a[:-1], i) and not seg.startswith(a, i):
                    put(a, style[a]); i += len(a) - 1; break      # ✔ без «вариант-селектора»
            else:
                put(seg[i]); i += 1

    last = 0
    for m in _WORD.finditer(text):
        plain(text[last:m.start()])
        font = (m.group(2) or "основной").strip().lower()
        abc = letters.get(font) or letters.get("основной") or {}
        main = letters.get("основной") or {}
        for ch in m.group(1).upper():
            if ch == " ":
                put("  ")                                    # пробел между словами, как у Марьи
            elif ch in abc:
                put(KEYCAP.get(ch, "🔤") if ch.isdigit() else "🔤", abc[ch])
            elif ch in main:                                 # в «зелёном» нет цифр — берём из основного
                put(KEYCAP.get(ch, "🔤") if ch.isdigit() else "🔤", main[ch])
            else:
                put(ch)
        last = m.end()
    plain(text[last:])
    return "".join(out), ents


def render(text: str):
    """Для отправки через telethon: (текст, список MessageEntityCustomEmoji)."""
    from telethon.tl.types import MessageEntityCustomEmoji
    t, ents = build(text)
    return t, [MessageEntityCustomEmoji(offset=o, length=l, document_id=d) for o, l, d in ents]


def preview_html(text: str) -> str:
    """Как пост будет выглядеть: премиум-эмодзи — картинками, остальное — текстом."""
    from html import escape
    t, ents = build(text)
    b = t.encode("utf-16-le")
    out, pos = [], 0
    for o, l, d in ents:
        out.append(escape(b[pos * 2:o * 2].decode("utf-16-le")))
        alt = b[o * 2:(o + l) * 2].decode("utf-16-le")
        out.append(f'<img class="tgemoji" src="/emoji/{d}" alt="{escape(alt)}">' if (DIR / f"{d}.thumb").exists()
                   else escape(alt))
        pos = o + l
    out.append(escape(b[pos * 2:].decode("utf-16-le")))
    return "".join(out)


def from_message(text: str, emojis: list | None) -> str:
    """Сообщение из чата (текст + номера картинок) → текст с [[СЛОВАМИ]] для примеров ИИ:
    подряд идущие буквы одного шрифта сворачиваются в [[СЛОВО]] / [[СЛОВО|зелёный]]."""
    if not emojis:
        return text
    _, letters = _maps()
    rev = {doc: (font, ch) for font, m in letters.items() for ch, doc in m.items()}
    b = text.encode("utf-16-le")
    out, pos, word, wfont = [], 0, [], None

    def flush():
        nonlocal word, wfont
        if word:
            w = re.sub(r"\s+", " ", "".join(word)).strip()
            out.append(f"[[{w}]]" if wfont == "основной" else f"[[{w}|{wfont}]]")
        word, wfont = [], None

    for o, l, d in sorted(emojis):
        gap = b[pos * 2:o * 2].decode("utf-16-le")
        hit = rev.get(int(d))
        if word and hit and hit[0] == wfont and gap.strip() == "":
            word.append(" " if gap else "")
        else:
            flush()
            out.append(gap)
        if hit:
            wfont = hit[0]
            word.append(hit[1])
        else:
            out.append(b[o * 2:(o + l) * 2].decode("utf-16-le"))
        pos = o + l
    flush()
    out.append(b[pos * 2:].decode("utf-16-le"))
    return "".join(out)


def message_html(text: str, emojis: list | None) -> str:
    """Сообщение из чата как есть: премиум-эмодзи — картинками (если картинка
    уже скачана), остальное — текстом. Переносы строк сохраняются в шаблоне."""
    from html import escape
    if not emojis:
        return escape(text or "")
    b = (text or "").encode("utf-16-le")
    out, pos = [], 0
    for o, l, d in sorted(emojis):
        if o < pos:
            continue
        out.append(escape(b[pos * 2:o * 2].decode("utf-16-le", "replace")))
        alt = b[o * 2:(o + l) * 2].decode("utf-16-le", "replace")
        out.append(f'<img class="tgemoji" src="/emoji/{d}" alt="{escape(alt)}">' if (DIR / f"{d}.thumb").exists()
                   else escape(alt))
        pos = o + l
    out.append(escape(b[pos * 2:].decode("utf-16-le", "replace")))
    return "".join(out)
