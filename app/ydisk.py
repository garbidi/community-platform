"""Разборы с Яндекс Диска: организатор вставляет ссылку на папку, платформа
находит папки «N РАЗБОР Фамилия Имя», сопоставляет с участниками потока и
после подтверждения по одной скачивает записи и ставит в очередь разборов.

Берём только звук (mp3, m4a, wav…); видео рядом по 4–10 ГБ не трогаем.
Скачиваем, только когда в очереди на расшифровку не больше QUEUE_MAX уже
скачанных записей, а после расшифровки исходник удаляем — wav по 2–3 ГБ не
копятся на диске. Заново его можно скачать с того же Диска.

Яндекс иногда отвечает 500 «file IO failure» — повторяем с паузами.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.parse
import urllib.request
from typing import Any

from . import reviews, store

API = "https://cloud-api.yandex.net/v1/disk/public/resources"
AUDIO = (".mp3", ".m4a", ".ogg", ".opus", ".aac", ".flac", ".wav")   # по порядку предпочтения
QUEUE_MAX = 2                 # скачанных, но ещё не расшифрованных записей
TRIES = 6                     # попыток скачать, паузы 1, 2, 4… минуты

SCHEMA = """
CREATE TABLE IF NOT EXISTS disk_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_id    INTEGER NOT NULL,
    url        TEXT NOT NULL,            -- публичная ссылка на папку
    path       TEXT NOT NULL,            -- путь файла внутри неё
    folder     TEXT NOT NULL,            -- «4 РАЗБОР Зарубина Мария»
    day        TEXT,                     -- «ВЕРШИНА 1 ДЕНЬ»
    size       INTEGER,
    person_id  INTEGER,                  -- карточка (people.id)
    match      TEXT,                     -- точно | похоже | нет
    status     TEXT NOT NULL DEFAULT 'найден', -- найден | в очереди | скачивание | передан | ошибка | пропущен
    review_id  INTEGER,
    error      TEXT,
    tries      INTEGER NOT NULL DEFAULT 0,
    next_at    TEXT,                     -- после сбоя Яндекса — не раньше
    added_by   TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (flow_id, url, path)
);
"""

_wake = threading.Event()


def init(conn) -> None:
    conn.executescript(SCHEMA)


# ----------------------------------------------------------------- Яндекс Диск

def _get(params: dict, path: str = "") -> dict:
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{API}{path}?{q}", headers={"User-Agent": "vershina-platform"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def _list(url: str, path: str = "/") -> list[dict]:
    out, offset = [], 0
    while True:
        r = _get({"public_key": url, "path": path, "limit": 200, "offset": offset})
        items = r.get("_embedded", {}).get("items", [])
        out += items
        if len(items) < 200:
            return out
        offset += 200


def check_url(url: str) -> str:
    url = (url or "").strip()
    if not re.match(r"https://(disk\.yandex\.(ru|com)|yadi\.sk)/", url):
        raise ValueError("Нужна публичная ссылка на папку Яндекс Диска (https://disk.yandex.ru/d/…).")
    return url


# ----------------------------------------------------------------- сопоставление

_FOLDER = re.compile(r"^\s*\d+\s*разбор\s*", re.I)


def _words(s: str) -> list[str]:
    s = (s or "").lower().replace("ё", "е")
    return [w for w in re.findall(r"[a-zа-я]+", s) if len(w) > 1]


def who_of(folder: str) -> str:
    """«4 РАЗБОР  Федоренко Иван» → «Федоренко Иван»."""
    return _FOLDER.sub("", folder).strip() if _FOLDER.match(folder) else ""


def match(name: str, people: list) -> tuple[int | None, str]:
    """Карточка по имени из папки: все слова совпали у одного человека —
    «точно»; совпало одно слово или по началу (Логачев/Логачёв, Ирина/Ира) —
    «похоже»; иначе «нет»."""
    w = _words(name)
    if not w:
        return None, "нет"
    full = [p for p in people if set(w) <= set(_words(p["name"]))]
    if len(full) == 1:
        return full[0]["id"], "точно" if len(w) >= 2 else "похоже"
    near = [p for p in people
            if all(any(a[:4] == b[:4] for b in _words(p["name"])) for a in w)]
    if len(near) == 1:
        return near[0]["id"], "похоже"
    return None, "нет"


def people_of(conn, flow) -> list:
    return conn.execute("SELECT id, name FROM people WHERE flow=? ORDER BY name", (flow["name"],)).fetchall()


def scan(conn, flow, url: str, who: str) -> dict:
    """Пройти папку (до трёх уровней), найти по одной записи на папку разбора."""
    url = check_url(url)
    people = people_of(conn, flow)
    found, seen = 0, 0

    def walk(path: str, depth: int, day: str | None):
        nonlocal found, seen
        items = _list(url, path)
        files = [i for i in items if i["type"] == "file"]
        folder = items and path.rstrip("/").rsplit("/", 1)[-1]
        name = who_of(folder) if folder else ""
        audio = sorted((f for f in files if f["name"].lower().endswith(AUDIO)),
                       key=lambda f: AUDIO.index("." + f["name"].lower().rsplit(".", 1)[-1]))
        if name and audio:
            seen += 1
            f = audio[0]
            pid, how = match(name, people)
            cur = conn.execute(
                "INSERT OR IGNORE INTO disk_items (flow_id, url, path, folder, day, size, person_id, match, added_by) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (flow["id"], url, f["path"], folder.strip(), day, f.get("size"), pid, how, who))
            found += cur.rowcount
        if depth < 3:
            for d in items:
                if d["type"] == "dir":
                    walk(d["path"], depth + 1, day or (d["name"] if depth == 0 and not who_of(d["name"]) else None))

    walk("/", 0, None)
    return {"seen": seen, "new": found}


def items(conn, flow_id: int) -> list:
    return conn.execute(
        "SELECT d.*, p.name AS person, r.status AS r_status, r.error AS r_error "
        "FROM disk_items d LEFT JOIN people p ON p.id = d.person_id "
        "LEFT JOIN reviews r ON r.id = d.review_id "
        "WHERE d.flow_id=? ORDER BY d.day, CAST(d.folder AS INTEGER), d.folder", (flow_id,)).fetchall()


def choose(conn, flow_id: int, picks: dict[int, tuple[int | None, bool]]) -> int:
    """Отметки организатора: {item: (карточка, загружать)}. Загружать — в очередь."""
    n = 0
    for iid, (pid, go) in picks.items():
        row = conn.execute("SELECT status FROM disk_items WHERE id=? AND flow_id=?", (iid, flow_id)).fetchone()
        if not row or row["status"] not in ("найден", "пропущен", "ошибка"):
            continue
        if go and pid:
            conn.execute("UPDATE disk_items SET person_id=?, status='в очереди', error=NULL, tries=0, next_at=NULL "
                         "WHERE id=?",
                         (pid, iid))
            n += 1
        elif pid:
            conn.execute("UPDATE disk_items SET person_id=? WHERE id=?", (pid, iid))
    if n:
        _wake.set()
    return n


def forget(conn, flow_id: int, iid: int) -> None:
    conn.execute("DELETE FROM disk_items WHERE id=? AND flow_id=? AND status IN ('найден','пропущен','ошибка')",
                 (iid, flow_id))


# ----------------------------------------------------------------- загрузка

def _pending(conn) -> int:
    """Скачано и ждёт расшифровки (исходник ещё на диске)."""
    return conn.execute(
        "SELECT COUNT(*) FROM disk_items d JOIN reviews r ON r.id = d.review_id "
        "WHERE d.status='передан' AND (r.transcript IS NULL OR r.transcript='') AND r.status!='ошибка'").fetchone()[0]


def _download(row) -> tuple:
    href = _get({"public_key": row["url"], "path": row["path"]}, "/download")["href"]
    tmp = reviews.REVIEW_DIR / f"disk-{row['id']}.part"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(href, headers={"User-Agent": "vershina-platform"})
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as out:
        while chunk := r.read(1 << 20):
            out.write(chunk)
    if row["size"] and tmp.stat().st_size != row["size"]:
        tmp.unlink()
        raise RuntimeError(f"скачалось {tmp.stat().st_size if tmp.exists() else 0} из {row['size']} байт")
    return tmp


def _step(conn) -> bool:
    """Одна запись из очереди. False — делать нечего."""
    # исходники расшифрованных — убрать (текст уже в базе)
    for r in conn.execute("SELECT d.review_id FROM disk_items d JOIN reviews r ON r.id = d.review_id "
                          "WHERE d.status='передан' AND r.transcript IS NOT NULL AND r.transcript!=''"):
        reviews.drop_source(r["review_id"])
    if _pending(conn) >= QUEUE_MAX:
        return False
    row = conn.execute("SELECT * FROM disk_items WHERE status='в очереди' "
                       "AND (next_at IS NULL OR next_at <= datetime('now')) ORDER BY id LIMIT 1").fetchone()
    if not row:
        return False
    conn.execute("UPDATE disk_items SET status='скачивание', error=NULL WHERE id=?", (row["id"],))
    try:
        tmp = _download(row)
    except Exception as exc:
        tries = row["tries"] + 1
        if tries >= TRIES:
            conn.execute("UPDATE disk_items SET status='ошибка', tries=?, error=? WHERE id=?",
                         (tries, f"не скачалось: {exc}"[:500], row["id"]))
        else:
            conn.execute("UPDATE disk_items SET status='в очереди', tries=?, error=?, "
                         "next_at=datetime('now', ?) WHERE id=?",
                         (tries, f"повтор {tries} из {TRIES - 1}: {exc}"[:500], f"+{2 ** (tries - 1)} minutes", row["id"]))
        return True
    title = f"Разбор · {row['day']}" if row["day"] else "Разбор"
    rid = reviews.create_from_file(conn, -row["person_id"], title, None, tmp,
                                   row["path"].rsplit("/", 1)[-1], row["added_by"] or "диск")
    conn.execute("UPDATE disk_items SET status='передан', review_id=?, error=NULL WHERE id=?", (rid, row["id"]))
    return True


def start_background() -> None:
    """Скачивание по одной записи (только в одном процессе — см. main)."""
    with store.connect() as conn:
        conn.execute("UPDATE disk_items SET status='в очереди' WHERE status='скачивание'")   # оборвались при перезапуске

    def loop():
        while True:
            try:
                with store.connect() as conn:
                    busy = _step(conn)
            except Exception:
                busy = False
            if not busy:
                _wake.wait(60)
                _wake.clear()
    threading.Thread(target=loop, daemon=True, name="disk-import").start()


def stats(rows: list) -> dict[str, Any]:
    c: dict[str, int] = {}
    for r in rows:
        k = r["status"]
        if k == "передан":
            k = {"готов": "готово", "ошибка": "ошибка разбора"}.get(r["r_status"], "в разборе")
        c[k] = c.get(k, 0) + 1
    return c
