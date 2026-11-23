"""Регистрация участников по ссылке через «Войти через Telegram».

Менеджер или ассистент сам присылает участнику ссылку (вживую, из своего
телеграма). Участник открывает её, нажимает «Войти через Telegram», телеграм
подтверждает, кто он, — и платформа получает его id, имя, @ник и фото.
Так у каждого участника с первого дня есть телеграм — не нужно потом
угадывать людей в чатах потока.

Три вида ссылок:
- одноразовая приглашающая — организатор создаёт по ссылке на человека и шлёт сам;
  по ней можно зарегистрироваться один раз, это заявка — пускает организатор
  (так заводят участников 4-дневного мастермайнда, без всяких таблиц);
- общая на поток — каждый вход становится заявкой, доступ открывает ассистент
  (если ссылка утекла, лишний человек дальше заявки не пройдёт; ссылку можно сменить);
- личная на участника — одноразовая: вход сразу связывает телеграм с его карточкой.

Под кнопкой — «бот-ключ» из @BotFather с доменом платформы. Он ни с кем не
переписывается: только подписывает данные входа. Имя и токен — в настройках
(«Управление» → «Телеграм»). Проверка подписи — по документации Telegram Login:
secret = SHA256(токен), hash = HMAC-SHA256(secret, «ключ=значение» по алфавиту).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
from datetime import datetime
from typing import Any

from . import flows, store

SCHEMA = """
CREATE TABLE IF NOT EXISTS join_links (
    token       TEXT PRIMARY KEY,
    flow_id     INTEGER NOT NULL,
    person_id   INTEGER,                 -- личная ссылка; NULL — общая на поток
    created_by  TEXT, created_at TEXT,
    revoked_at  TEXT,
    used_at     TEXT, used_tg INTEGER,   -- личная и одноразовая: когда и кем использована
    single      INTEGER NOT NULL DEFAULT 0,   -- 1 — одноразовая приглашающая
    note        TEXT                     -- кому отправили (видит только организатор)
);
CREATE TABLE IF NOT EXISTS join_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_id     INTEGER NOT NULL,
    link        TEXT,
    tg_id       INTEGER NOT NULL,
    first_name  TEXT, last_name TEXT, username TEXT, photo_url TEXT,
    full_name   TEXT, phone TEXT,        -- участник уточняет сам после входа
    secret      TEXT NOT NULL,           -- чтобы дописать ФИО и телефон мог только он
    status      TEXT NOT NULL DEFAULT 'ждёт',   -- ждёт | принята | отклонена
    photo       TEXT,                    -- фото, которое человек загрузил сам
    person_id   INTEGER,
    created_at  TEXT, decided_by TEXT, decided_at TEXT,
    UNIQUE (flow_id, tg_id)
);
"""

MAX_AGE = 24 * 3600          # данные входа старше суток не принимаем


class JoinError(ValueError):
    """Ошибка, которую можно показать человеку как есть."""


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    lcols = {r[1] for r in conn.execute("PRAGMA table_info(join_links)")}
    if "single" not in lcols:
        conn.execute("ALTER TABLE join_links ADD COLUMN single INTEGER NOT NULL DEFAULT 0")
    if "note" not in lcols:
        conn.execute("ALTER TABLE join_links ADD COLUMN note TEXT")
    if "photo" not in {r[1] for r in conn.execute("PRAGMA table_info(join_requests)")}:
        conn.execute("ALTER TABLE join_requests ADD COLUMN photo TEXT")
    if "tg_login_id" not in {r[1] for r in conn.execute("PRAGMA table_info(users)")}:
        # телеграм, которым человек входит кнопкой «Войти через Telegram»
        conn.execute("ALTER TABLE users ADD COLUMN tg_login_id INTEGER")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ----------------------------------------------------------------- бот-ключ

def bot(conn) -> dict[str, str]:
    return {"name": (store.get_setting(conn, "tg_login:bot", "") or "").lstrip("@"),
            "token": store.get_setting(conn, "tg_login:token", "") or ""}


def configured(conn) -> bool:
    b = bot(conn)
    return bool(b["name"] and b["token"])


def set_bot(conn, name: str, token: str) -> None:
    name = (name or "").strip().lstrip("@")
    token = (token or "").strip()
    if name and not name.lower().endswith("bot"):
        raise JoinError("Имя бота из @BotFather заканчивается на «bot», например vershina_login_bot.")
    store.set_setting(conn, "tg_login:bot", name)
    if token:                                  # пустое поле — токен не меняем
        if ":" not in token:
            raise JoinError("Токен выглядит как 1234567890:AA… — скопируйте его из @BotFather целиком.")
        store.set_setting(conn, "tg_login:token", token)


def verify(conn, data: dict[str, str]) -> dict[str, Any]:
    """Проверить данные, которые телеграм передал после «Войти через Telegram»."""
    token = bot(conn)["token"]
    if not token:
        raise JoinError("Вход через Telegram ещё не настроен.")
    got = (data.get("hash") or "").lower()
    fields = {k: v for k, v in data.items() if k != "hash" and v is not None}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    want = hmac.new(hashlib.sha256(token.encode()).digest(), check.encode(), hashlib.sha256).hexdigest()
    if not got or not hmac.compare_digest(want.encode(), got.encode()):
        raise JoinError("Телеграм не подтвердил вход — попробуйте ещё раз по ссылке.")
    try:
        age = time.time() - int(fields.get("auth_date", "0"))
    except ValueError:
        age = MAX_AGE + 1
    if age > MAX_AGE:
        raise JoinError("Вход устарел — откройте ссылку и нажмите кнопку ещё раз.")
    return {"tg_id": int(fields["id"]), "first_name": fields.get("first_name", ""),
            "last_name": fields.get("last_name", ""), "username": fields.get("username", ""),
            "photo_url": fields.get("photo_url", "")}


# ----------------------------------------------------------------- ссылки

def link(conn, token: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM join_links WHERE token=?", (token,)).fetchone()


def active_link(conn, token: str, tg_id: int | None = None) -> sqlite3.Row:
    ln = link(conn, token)
    if not ln or ln["revoked_at"]:
        raise JoinError("Ссылка недействительна. Попросите менеджера прислать новую.")
    if ln["single"] and ln["used_at"] and ln["used_tg"] != tg_id:
        raise JoinError("По этой ссылке уже зарегистрировались. Если это были вы — входите на платформу "
                        "кнопкой «Войти через Telegram»; если нет — попросите менеджера новую ссылку.")
    if ln["person_id"] and ln["used_at"]:
        raise JoinError("Эта личная ссылка уже использована. Если это были вы — входите на платформу "
                        "кнопкой «Войти через Telegram».")
    f = flows.get(conn, ln["flow_id"])
    if not f or f["status"] == "завершён":
        raise JoinError("Поток уже завершён — ссылка больше не действует.")
    return ln


def flow_link(conn, flow_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM join_links WHERE flow_id=? AND person_id IS NULL AND single=0 AND revoked_at IS NULL "
                        "ORDER BY created_at DESC LIMIT 1", (flow_id,)).fetchone()


def new_invites(conn, flow_id: int, notes: list[str], who: str) -> list[str]:
    """Одноразовые приглашающие ссылки — по одной на человека (note — кому, для себя)."""
    out = []
    for note in notes[:200]:
        token = secrets.token_urlsafe(9)
        conn.execute("INSERT INTO join_links (token, flow_id, created_by, created_at, single, note) VALUES (?,?,?,?,1,?)",
                     (token, flow_id, who, _now(), (note or "").strip()[:80] or None))
        out.append(token)
    return out


def invites(conn, flow_id: int) -> list[dict]:
    """Одноразовые ссылки потока: кому, использована ли, кем, чем кончилась заявка."""
    rows = []
    for ln in conn.execute("SELECT * FROM join_links WHERE flow_id=? AND single=1 AND revoked_at IS NULL "
                           "ORDER BY created_at DESC", (flow_id,)):
        r = conn.execute("SELECT * FROM join_requests WHERE link=? ORDER BY id DESC LIMIT 1", (ln["token"],)).fetchone()
        rows.append(dict(ln, req=dict(r) if r else None))
    return rows


def revoke_invite(conn, token: str) -> None:
    conn.execute("UPDATE join_links SET revoked_at=? WHERE token=? AND single=1", (_now(), token))


def new_flow_link(conn, flow_id: int, who: str) -> str:
    """Общая ссылка на поток. Прежняя перестаёт работать — так её «меняют»."""
    conn.execute("UPDATE join_links SET revoked_at=? WHERE flow_id=? AND person_id IS NULL AND revoked_at IS NULL",
                 (_now(), flow_id))
    token = secrets.token_urlsafe(9)
    conn.execute("INSERT INTO join_links (token, flow_id, created_by, created_at) VALUES (?,?,?,?)",
                 (token, flow_id, who, _now()))
    return token


def revoke_flow_link(conn, flow_id: int) -> None:
    conn.execute("UPDATE join_links SET revoked_at=? WHERE flow_id=? AND person_id IS NULL AND revoked_at IS NULL",
                 (_now(), flow_id))


def person_link(conn, pid: int, who: str) -> str:
    """Личная одноразовая ссылка участника: действующая или новая."""
    p = conn.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
    f = flows.get(conn, name=p["flow"]) if p else None
    if not f:
        raise JoinError("Участник не найден.")
    row = conn.execute("SELECT token FROM join_links WHERE person_id=? AND revoked_at IS NULL AND used_at IS NULL",
                       (pid,)).fetchone()
    if row:
        return row["token"]
    token = secrets.token_urlsafe(12)
    conn.execute("INSERT INTO join_links (token, flow_id, person_id, created_by, created_at) VALUES (?,?,?,?,?)",
                 (token, f["id"], pid, who, _now()))
    return token


# ----------------------------------------------------------------- вход по ссылке

def _participant_user(conn, pid: int, tg: dict) -> int:
    """Учётная запись участника: видит свою карточку, входит своим телеграмом.
    Пароля нет — только «Войти через Telegram»."""
    from .roster import card_id
    card = card_id(pid)
    u = conn.execute("SELECT id FROM users WHERE tg_login_id=? OR (role='participant' AND tg_user_id=?)",
                     (tg["tg_id"], card)).fetchone()
    name = conn.execute("SELECT name FROM people WHERE id=?", (pid,)).fetchone()["name"]
    if u:
        conn.execute("UPDATE users SET tg_login_id=?, tg_user_id=?, active=1 WHERE id=?", (tg["tg_id"], card, u["id"]))
        return u["id"]
    login = f"tg{tg['tg_id']}"
    uid = store.create_user(conn, login, name, "participant", secrets.token_urlsafe(24), card)
    conn.execute("UPDATE users SET tg_login_id=? WHERE id=?", (tg["tg_id"], uid))
    return uid


def bind(conn, pid: int, tg: dict, who: str) -> int:
    """Связать телеграм с карточкой участника и открыть ему вход."""
    taken = conn.execute("SELECT name, flow FROM people WHERE tg_user_id=? AND id<>? AND flow=(SELECT flow FROM "
                         "people WHERE id=?)", (tg["tg_id"], pid, pid)).fetchone()
    if taken:
        raise JoinError(f"Этот телеграм уже связан с участником «{taken['name']}» потока {taken['flow']}.")
    conn.execute("UPDATE people SET tg_user_id=?, username=COALESCE(NULLIF(?, ''), username) WHERE id=?",
                 (tg["tg_id"], tg.get("username") or "", pid))
    uid = _participant_user(conn, pid, tg)
    from . import fourday
    own = conn.execute("SELECT photo FROM join_requests WHERE tg_id=? AND photo IS NOT NULL ORDER BY id DESC LIMIT 1",
                       (tg["tg_id"],)).fetchone()
    if own and (fourday.PHOTOS / own["photo"]).exists():
        fourday.save_photo(conn, pid, (fourday.PHOTOS / own["photo"]).read_bytes())
    elif tg.get("photo_url"):
        fourday.photo_from_url(conn, pid, tg["photo_url"])
    conn.execute("UPDATE join_requests SET status='принята', person_id=?, decided_by=?, decided_at=? "
                 "WHERE tg_id=? AND status='ждёт' AND flow_id=(SELECT f.id FROM flows f JOIN people p ON p.flow=f.name "
                 "WHERE p.id=?)", (pid, who, _now(), tg["tg_id"], pid))
    return uid


def arrive(conn, token: str, tg: dict) -> dict[str, Any]:
    """Человек вошёл по ссылке. Личная — сразу в карточку; общая — заявка
    (или вход, если он уже участник этого потока)."""
    ln = active_link(conn, token, tg["tg_id"])
    if ln["single"] and not ln["used_at"]:              # одноразовая — больше никто по ней не пройдёт
        conn.execute("UPDATE join_links SET used_at=?, used_tg=? WHERE token=?", (_now(), tg["tg_id"], token))
    if ln["person_id"]:
        uid = bind(conn, ln["person_id"], tg, "по личной ссылке")
        conn.execute("UPDATE join_links SET used_at=?, used_tg=? WHERE token=?", (_now(), tg["tg_id"], token))
        return {"kind": "вход", "user_id": uid}
    u = conn.execute("SELECT u.* FROM users u WHERE u.tg_login_id=? AND u.active=1", (tg["tg_id"],)).fetchone()
    f = flows.get(conn, ln["flow_id"])
    if u and conn.execute("SELECT 1 FROM people WHERE flow=? AND tg_user_id=?", (f["name"], tg["tg_id"])).fetchone():
        return {"kind": "вход", "user_id": u["id"]}
    from . import fourday
    hit = fourday.match(conn, f["name"], username=tg.get("username"))
    if hit:                                            # @ник совпал с карточкой — сразу в неё
        uid = bind(conn, hit["id"], tg, "по совпадению @ника")
        return {"kind": "вход", "user_id": uid}
    r = conn.execute("SELECT * FROM join_requests WHERE flow_id=? AND tg_id=?", (f["id"], tg["tg_id"])).fetchone()
    if r:
        conn.execute("UPDATE join_requests SET first_name=?, last_name=?, username=?, photo_url=? WHERE id=?",
                     (tg["first_name"], tg["last_name"], tg["username"], tg["photo_url"], r["id"]))
        return {"kind": "заявка", "request": r["id"], "secret": r["secret"], "status": r["status"]}
    sec = secrets.token_urlsafe(16)
    rid = int(conn.execute(
        "INSERT INTO join_requests (flow_id, link, tg_id, first_name, last_name, username, photo_url, full_name, "
        "secret, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (f["id"], token, tg["tg_id"], tg["first_name"], tg["last_name"], tg["username"], tg["photo_url"],
         f"{tg['last_name']} {tg['first_name']}".strip(), sec, _now())).lastrowid)
    return {"kind": "заявка", "request": rid, "secret": sec, "status": "ждёт"}


def request_by_secret(conn, rid: int, secret: str) -> sqlite3.Row:
    r = conn.execute("SELECT * FROM join_requests WHERE id=?", (rid,)).fetchone()
    if not r or not hmac.compare_digest(r["secret"].encode(), (secret or "").encode()):
        raise JoinError("Заявка не найдена.")
    return r


def complete(conn, rid: int, secret: str, full_name: str, phone: str, photo: bytes | None = None) -> bool:
    """Участник уточняет ФИО, телефон и фото. Телефон совпал с карточкой потока —
    доступ открывается сразу (True)."""
    r = request_by_secret(conn, rid, secret)
    import re
    digits = re.sub(r"\D", "", phone or "")
    if digits and not 10 <= len(digits) <= 15:
        raise JoinError("Телефон — 10–15 цифр, например +7 900 123-45-67.")
    conn.execute("UPDATE join_requests SET full_name=?, phone=? WHERE id=?",
                 (re.sub(r"\s+", " ", full_name or "").strip()[:80] or None, digits or None, rid))
    from . import fourday
    if photo:
        fourday.PHOTOS.mkdir(parents=True, exist_ok=True)
        name = f"req{rid}.jpg"
        (fourday.PHOTOS / name).write_bytes(photo)
        conn.execute("UPDATE join_requests SET photo=? WHERE id=?", (name, rid))
        if r["person_id"]:
            fourday.save_photo(conn, r["person_id"], photo)
    if r["status"] == "ждёт" and digits:
        f = flows.get(conn, r["flow_id"])
        hit = fourday.match(conn, f["name"], phone=digits)
        if hit:
            bind(conn, hit["id"], {"tg_id": r["tg_id"], "username": r["username"], "photo_url": r["photo_url"]},
                 "по совпадению телефона")
            return True
    return False


# ----------------------------------------------------------------- очередь

def _key(text: str) -> frozenset:
    import re
    return frozenset(w for w in re.findall(r"[а-яёa-z]+", (text or "").lower().replace("ё", "е")) if len(w) > 1)


def pending(conn, flow_id: int) -> list[dict]:
    """Заявки, которые ждут ассистента, — с подсказкой, кто это из потока."""
    f = flows.get(conn, flow_id)
    free = conn.execute("SELECT p.id, p.name, t.title AS team FROM people p LEFT JOIN teams t ON t.id=p.team_id "
                        "WHERE p.flow=? AND p.tg_user_id IS NULL AND p.status='в потоке' ORDER BY p.name",
                        (f["name"],)).fetchall()
    out = []
    for r in conn.execute("SELECT * FROM join_requests WHERE flow_id=? AND status='ждёт' ORDER BY created_at",
                          (flow_id,)):
        names = _key(r["full_name"]) | _key(f"{r['first_name']} {r['last_name']}")
        hits = [p for p in free if len(_key(p["name"]) & names) >= 2]
        out.append(dict(r, guess=hits[0] if len(hits) == 1 else None, free=free))
    return out


def decided(conn, flow_id: int, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute("SELECT r.*, p.name AS person FROM join_requests r LEFT JOIN people p ON p.id=r.person_id "
                        "WHERE r.flow_id=? AND r.status<>'ждёт' ORDER BY r.decided_at DESC LIMIT ?",
                        (flow_id, limit)).fetchall()


def approve(conn, rid: int, who: str, person_id: int | None = None, team_id: int | None = None) -> int:
    """Пустить: связать с участником потока или завести нового в команду на платформе."""
    from . import decl
    r = conn.execute("SELECT * FROM join_requests WHERE id=?", (rid,)).fetchone()
    if not r or r["status"] != "ждёт":
        raise JoinError("Заявка уже разобрана.")
    tg = {"tg_id": r["tg_id"], "username": r["username"]}
    f = flows.get(conn, r["flow_id"])
    if not person_id:
        from . import fourday
        name = r["full_name"] or f"{r['last_name']} {r['first_name']}".strip() or (r["username"] or f"tg{r['tg_id']}")
        try:
            if fourday.is_fourday(f):                  # 4-дневный мастермайнд — без команд
                person_id = decl.add_loose(conn, f["id"], name, r["username"] or "", r["phone"] or "")
            elif team_id:
                person_id = decl.add_person(conn, team_id, name, r["username"] or "", 0)
            else:
                raise JoinError("Выберите участника из списка или команду для нового.")
        except decl.DeclError as e:
            raise JoinError(str(e))
        if r["photo_url"] or r["photo"]:
            tg["photo_url"] = r["photo_url"]
    p = conn.execute("SELECT flow FROM people WHERE id=?", (person_id,)).fetchone()
    if not p or p["flow"] != f["name"]:
        raise JoinError("Участник не из этого потока.")
    bind(conn, person_id, tg, who)
    conn.execute("UPDATE join_requests SET status='принята', person_id=?, decided_by=?, decided_at=? WHERE id=?",
                 (person_id, who, _now(), rid))
    return person_id


def reject(conn, rid: int, who: str) -> None:
    conn.execute("UPDATE join_requests SET status='отклонена', decided_by=?, decided_at=? WHERE id=? AND status='ждёт'",
                 (who, _now(), rid))


def login_user(conn, tg: dict) -> sqlite3.Row | None:
    """Вход кнопкой на странице входа: по телеграму, которым человек уже входил
    (или который организатор указал в его учётной записи)."""
    return conn.execute("SELECT * FROM users WHERE tg_login_id=? AND active=1", (tg["tg_id"],)).fetchone()
