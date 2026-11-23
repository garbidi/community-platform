"""Текст «как написано» → текст «как читается».

Silero молча пропускает цифры и латиницу, поэтому в голосовом ответе
исчезали суммы, даты, время и слова вроде Zoom. Здесь всё это
переписывается русскими словами до синтеза:

    15.09 в 18:00, 82 вовлечения, 98,7%, 125 000 ₽, +1, Zoom и Google
    → пятнадцатого сентября в восемнадцать ноль-ноль, восемьдесят два
      вовлечения, девяносто восемь целых семь десятых процента, сто
      двадцать пять тысяч рублей, плюс один, зум и гугл
"""

from __future__ import annotations

import re

# ----------------------------------------------------------- числительные

_UNITS = ["", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять",
          "десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать",
          "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать"]
_UNITS_F = {1: "одна", 2: "две"}
_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят",
         "восемьдесят", "девяносто"]
_HUNDREDS = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот",
             "восемьсот", "девятьсот"]
# тысячи — женского рода, остальные разряды — мужского
_SCALES = [("тысяча", "тысячи", "тысяч", True), ("миллион", "миллиона", "миллионов", False),
           ("миллиард", "миллиарда", "миллиардов", False)]


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 19:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def _triplet(n: int, feminine: bool) -> str:
    words = []
    if n >= 100:
        words.append(_HUNDREDS[n // 100])
        n %= 100
    if n >= 20:
        words.append(_TENS[n // 10])
        n %= 10
    if n:
        words.append(_UNITS_F[n] if feminine and n in _UNITS_F else _UNITS[n])
    return " ".join(words)


def cardinal(n: int, feminine: bool = False) -> str:
    if n == 0:
        return "ноль"
    if n < 0:
        return "минус " + cardinal(-n, feminine)
    parts, scale = [], 0
    while n:
        n, chunk = divmod(n, 1000)
        if chunk:
            if scale == 0:
                parts.append(_triplet(chunk, feminine))
            else:
                one, few, many, fem = _SCALES[scale - 1]
                parts.append(f"{_triplet(chunk, fem)} {plural(chunk, one, few, many)}")
        scale += 1
    return " ".join(reversed(parts))


_ORD = {1: "перв", 2: "втор", 3: "трет", 4: "четверт", 5: "пят", 6: "шест", 7: "седьм",
        8: "восьм", 9: "девят", 10: "десят", 11: "одиннадцат", 12: "двенадцат",
        13: "тринадцат", 14: "четырнадцат", 15: "пятнадцат", 16: "шестнадцат",
        17: "семнадцат", 18: "восемнадцат", 19: "девятнадцат", 20: "двадцат", 30: "тридцат",
        40: "сороков", 50: "пятидесят", 60: "шестидесят", 70: "семидесят", 80: "восьмидесят",
        90: "девяност", 100: "сот"}


def ordinal(n: int, ending: str = "ый") -> str:
    """Порядковое до 199: «пятнадцат» + окончание. Для 3 окончание мягкое:
    третий/третье/третьего."""
    if n >= 100:
        head = "сто " if n < 200 else cardinal(n // 100 * 100) + " "
        return head + ordinal(n % 100, ending) if n % 100 else cardinal(n)
    if n in _ORD:
        stem = _ORD[n]
    elif n > 20 and n % 10:
        return f"{_TENS[n // 10]} {ordinal(n % 10, ending)}"
    else:
        return cardinal(n)
    if n == 3:
        return stem + {"ый": "ий", "ое": "ье", "ого": "ьего", "ая": "ья"}.get(ending, ending)
    if n in (2, 6, 7, 8) and ending == "ый":
        return stem + "ой"
    if n == 40 and ending == "ый":
        return stem + "ой"
    return stem + ending


def fraction(whole: int, frac: str) -> str:
    """98,7 → «девяносто восемь целых семь десятых»."""
    denom = {1: ("десятая", "десятых"), 2: ("сотая", "сотых"), 3: ("тысячная", "тысячных")}
    if len(frac) not in denom:
        return cardinal(whole) + " и " + " ".join(cardinal(int(d)) for d in frac)
    num = int(frac)
    one, many = denom[len(frac)]
    return (f"{cardinal(whole, True)} {plural(whole, 'целая', 'целых', 'целых')} "
            f"{cardinal(num, True)} {one if num % 10 == 1 and num % 100 != 11 else many}")


# ---------------------------------------------------------------- подстановки

_MONTHS = ["", "января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
           "сентября", "октября", "ноября", "декабря"]

_WORDS = {
    "zoom": "зум", "google": "гугл", "telegram": "телеграм", "tg": "тэ-гэ", "word": "ворд",
    "excel": "эксель", "pdf": "пэ-дэ-эф", "docx": "ворд", "ok": "окей", "okay": "окей",
    "id": "ай-ди", "api": "а-пэ-и", "wifi": "вай-фай", "wi-fi": "вай-фай", "youtube": "ютуб",
    "iphone": "айфон", "android": "андроид", "windows": "виндоус", "linux": "линукс",
    "python": "пайтон", "claude": "клод", "chatgpt": "чат-джи-пи-ти", "gpt": "джи-пи-ти",
    "whatsapp": "вотсап", "instagram": "инстаграм", "vk": "вэ-ка", "sms": "эс-эм-эс",
    "email": "имейл", "e-mail": "имейл", "vs": "против", "ai": "эй-ай", "it": "ай-ти",
    "yandex": "яндекс", "sber": "сбер", "ozon": "озон", "wildberries": "вайлдберриз",
    "mm": "эм-эм", "rzd": "эр-жэ-дэ", "cv": "си-ви", "hr": "эйч-ар", "ceo": "си-и-оу",
}

# латиница по буквам и сочетаниям — грубо, но слышно хоть что-то
_DIGRAPHS = [("sch", "ш"), ("tsch", "ч"), ("sh", "ш"), ("ch", "ч"), ("th", "т"), ("ph", "ф"),
             ("kh", "х"), ("zh", "ж"), ("ts", "ц"), ("ya", "я"), ("yu", "ю"), ("yo", "ё"),
             ("ye", "е"), ("oo", "у"), ("ee", "и"), ("ck", "к"), ("qu", "кв"),
             ("ai", "эй"), ("ay", "эй"), ("ey", "эй"), ("ow", "оу"), ("ou", "ау")]
_LETTERS = {"a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "х",
            "i": "и", "j": "дж", "k": "к", "l": "л", "m": "м", "n": "н", "o": "о", "p": "п",
            "q": "к", "r": "р", "s": "с", "t": "т", "u": "у", "v": "в", "w": "в", "x": "кс",
            "y": "й", "z": "з"}


def translit(word: str) -> str:
    low = word.lower()
    if low in _WORDS:
        return _WORDS[low]
    if len(low) <= 3 and word.isupper():           # аббревиатура: по буквам
        names = {"a": "а", "b": "би", "c": "си", "d": "ди", "e": "и", "f": "эф", "g": "джи",
                 "h": "эйч", "i": "ай", "j": "джей", "k": "кей", "l": "эл", "m": "эм",
                 "n": "эн", "o": "оу", "p": "пи", "q": "кью", "r": "ар", "s": "эс", "t": "ти",
                 "u": "ю", "v": "ви", "w": "дабл-ю", "x": "икс", "y": "уай", "z": "зед"}
        return "-".join(names[c] for c in low if c in names)
    out = low
    for a, b in _DIGRAPHS:
        out = out.replace(a, b)
    out = re.sub(r"y$", "и", out)                  # Anatoly → анатоли
    out = re.sub(r"y(?=[bcdfghjklmnpqrstvwxz])", "и", out)   # Lyudmila → людмила
    out = "".join(_LETTERS.get(c, c) for c in out)
    return out


def _money(m: re.Match) -> str:
    n = int(m.group(1).replace(" ", "").replace(" ", ""))
    return f"{cardinal(n)} {plural(n, 'рубль', 'рубля', 'рублей')}"


def _percent(m: re.Match) -> str:
    whole, frac = m.group(1), m.group(2)
    if frac:
        return f"{fraction(int(whole), frac)} процента"
    n = int(whole)
    return f"{cardinal(n)} {plural(n, 'процент', 'процента', 'процентов')}"


def _date(m: re.Match) -> str:
    d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
    if not 1 <= mo <= 12 or not 1 <= d <= 31:
        return m.group(0)
    out = f"{ordinal(d, 'ого')} {_MONTHS[mo]}"
    if y:
        out += f" {_year(int(y))} года"
    return out


def _year(y: int) -> str:
    """2026 → «две тысячи двадцать шестого»."""
    if y >= 2000:
        head = "две тысячи" if y < 3000 else cardinal(y // 1000 * 1000)
        rest = y % 1000
        return f"{head} {ordinal(rest, 'ого')}" if rest else "двухтысячного"
    return cardinal(y)


def _time(m: re.Match) -> str:
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return m.group(0)
    return f"{cardinal(h)} {'ноль-ноль' if mi == 0 else cardinal(mi)}"


def _ordinal_suffix(m: re.Match) -> str:
    n, suf = int(m.group(1)), m.group(2)
    endings = {"й": "ый", "е": "ое", "го": "ого", "я": "ая", "ю": "ую", "х": "ых", "м": "ом"}
    return ordinal(n, endings.get(suf, "ый"))


def _plain(m: re.Match) -> str:
    digits = m.group(0).replace(" ", "").replace(" ", "")
    if len(digits) > 7:                        # номера, коды — по цифрам
        return " ".join(cardinal(int(d)) for d in digits)
    return cardinal(int(digits))


def spoken(text: str) -> str:
    t = text
    t = re.sub(r"(\d{1,3}(?:[  ]\d{3})+|\d+)\s*(?:₽|руб\.?|рублей|р\.)(?!\w)", _money, t)
    t = re.sub(r"(\d+)(?:[.,](\d+))?\s*%", _percent, t)
    t = re.sub(r"\b(\d{1,2})\.(\d{2})(?:\.(\d{4}))?\b", _date, t)
    t = re.sub(r"\b(\d{1,2}):(\d{2})\b", _time, t)
    t = re.sub(r"\b(\d+)-(го|й|е|я|ю|х|м)\b", _ordinal_suffix, t)
    t = re.sub(r"(\d+)[.,](\d+)", lambda m: fraction(int(m.group(1)), m.group(2)), t)
    t = re.sub(r"(?<![\w-])\+\s*(?=\d)", "плюс ", t)
    t = re.sub(r"(?<![\w-])[-−–]\s*(?=\d)", "минус ", t)
    t = re.sub(r"\d{1,3}(?:[  ]\d{3})+|\d+", _plain, t)
    t = re.sub(r"[A-Za-z][A-Za-z'\-]*", lambda m: translit(m.group(0)), t)
    t = re.sub(r"(?<=[а-яё])(?=[А-ЯЁ])", " ", t)     # «девятьЭ» → «девять Э»
    return t
