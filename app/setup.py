"""Мастер первоначальной настройки.

Хозяин помощника — не технарь: его путь должен укладываться в «открыть
ссылку и заполнить форму». Всё, что можно определить самим, определяем
сами (телеграм-id ловим по первому сообщению боту), всё остальное просим
по одному за шаг и сразу проверяем, чтобы ошибка всплывала на этом же
экране, а не через сутки молчания.

Состояние живёт в таблице settings базы приложения под ключами setup:*,
а сами настройки мастер пишет в тот же bot.env, что читают бот и скрипты
рабочего каталога: службы следят за временем его изменения и
перезапускаются сами.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import subprocess
import time
from pathlib import Path
from typing import Any

from . import store

BOT_ENV = Path(os.environ.get(
    "VERSHINA_BOT_ENV",
    str(Path(__file__).resolve().parent.parent / "bot.env")))
KB_ENV = Path(os.environ.get(
    "VERSHINA_KB_ENV",
    str(Path(os.environ.get("VERSHINA_KB_SRC", "/opt/vershina-kb")) / ".env")))
SESSION_DIR = Path(os.environ.get("VERSHINA_KB_DATA", "/data/kb")) / "session"

# Шаги мастера по порядку. Последний — проверка, что всё вместе работает.
STEPS = [
    ("account", "Ваш вход"),
    ("bot", "Бот в телеграме"),
    ("owner", "Это вы"),
    ("telegram", "Ваш телеграм"),
    ("claude", "Голова помощника"),
    ("extras", "Календарь и встречи"),
    ("check", "Проверка"),
]
STEP_KEYS = [s[0] for s in STEPS]


# --------------------------------------------------------------- состояние

def _conn():
    return store.connect()


def get(key: str, default: str | None = None) -> str | None:
    conn = _conn()
    try:
        return store.get_setting(conn, f"setup:{key}", default)
    finally:
        conn.close()


def put(key: str, value: str) -> None:
    conn = _conn()
    try:
        store.set_setting(conn, f"setup:{key}", value)
        conn.commit()
    finally:
        conn.close()


# Установка, настроенная до появления мастера, тоже считается настроенной.
# Флаг кешируем: заслон спрашивает его на каждом запросе, а обратно в
# «не настроено» состояние не возвращается.
_done_cache = False


def is_done() -> bool:
    global _done_cache
    if _done_cache:
        return True
    if get("done") == "1":
        _done_cache = True
        return True
    if _looks_configured():
        put("done", "1")
        _done_cache = True
        return True
    return False


def _looks_configured() -> bool:
    """Признак установки, поднятой до мастера: пользователи уже есть, а
    мастер ни разу не начинали.

    Проверять только по наличию пользователей нельзя: мастер заводит
    учётку на первом же шаге, и тогда он сам себя объявил бы законченным.
    Отличает то, что мастер всегда отмечает текущий шаг.
    """
    if get("step") is not None:
        return False
    conn = _conn()
    try:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0
    except Exception:
        return False
    finally:
        conn.close()


def current_step() -> str:
    return get("step", STEP_KEYS[0]) or STEP_KEYS[0]


def advance(step: str) -> str:
    """Переводит мастер на шаг после указанного и возвращает новый."""
    i = STEP_KEYS.index(step)
    nxt = STEP_KEYS[i + 1] if i + 1 < len(STEP_KEYS) else "check"
    put("step", nxt)
    return nxt


def setup_link_token() -> str:
    """Одноразовый ключ на ссылку мастера. Нужен, чтобы настройку не прошёл
    случайный человек, наткнувшийся на адрес раньше хозяина."""
    have = get("token")
    if have:
        return have
    fresh = secrets.token_urlsafe(24)
    put("token", fresh)
    return fresh


def key_ok(key: str | None) -> bool:
    if is_done():
        return False
    want = setup_link_token()
    return bool(key) and secrets.compare_digest(str(key).encode(), want.encode())


# ------------------------------------------------------------- запись env

def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def write_env(path: Path, updates: dict[str, str]) -> None:
    """Правит файл настроек, сохраняя комментарии и порядок строк.

    Пишем через временный файл рядом: службы следят за временем изменения,
    и увидеть наполовину записанный файл они не должны.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        m = re.match(r"^(\w+)=", line.strip())
        if m and m.group(1) in updates:
            key = m.group(1)
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ------------------------------------------------------------------ бот

def check_bot_token(token: str) -> dict[str, Any]:
    """Спрашивает у телеграма, живой ли токен. Возвращает имя бота."""
    import httpx
    token = token.strip()
    if not re.match(r"^\d+:[\w-]{30,}$", token):
        return {"ok": False, "error":
                "Это не похоже на токен. Он выглядит так: 1234567890:AAE…"}
    try:
        r = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15)
        data = r.json()
    except Exception:
        return {"ok": False, "error":
                "Телеграм не ответил. Проверьте связь и попробуйте ещё раз."}
    if not data.get("ok"):
        return {"ok": False, "error":
                "Телеграм не признал этот токен. Скопируйте его из "
                "переписки с @BotFather ещё раз, целиком."}
    me = data["result"]
    return {"ok": True, "username": me.get("username"), "name": me.get("first_name")}


def catch_owner(token: str, since: float) -> dict[str, Any] | None:
    """Ищет первого, кто написал боту после начала шага.

    Так хозяину не приходится узнавать и вводить свой телеграм-id: он
    просто пишет боту слово, а мы забираем отправителя.
    """
    import httpx
    try:
        r = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates",
                      params={"timeout": 0, "limit": 20}, timeout=20)
        data = r.json()
    except Exception:
        return None
    if not data.get("ok"):
        return None
    for upd in data.get("result", []):
        msg = upd.get("message") or {}
        if not msg or msg.get("date", 0) < since:
            continue
        frm = msg.get("from") or {}
        if frm.get("is_bot"):
            continue
        name = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x)
        return {"id": frm.get("id"), "name": name or "без имени",
                "username": frm.get("username")}
    return None


def send_message(token: str, chat_id: int, text: str) -> bool:
    import httpx
    try:
        r = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                       json={"chat_id": chat_id, "text": text}, timeout=15)
        return bool(r.json().get("ok"))
    except Exception:
        return False


# ------------------------------------------------------------- телеграм

def _tg_client(loop_session: str = "vershina"):
    """Клиент Telethon на файл сессии в томе.

    Объект между запросами не держим: каждый шаг создаёт свой клиент поверх
    того же файла сессии, а промежуточное состояние (phone_code_hash)
    храним в настройках. Так мастер переживает перезапуск приложения.
    """
    from telethon import TelegramClient
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    api_id = int(os.environ.get("TG_API_ID", "0") or 0)
    api_hash = os.environ.get("TG_API_HASH", "")
    if not api_id or not api_hash:
        raise RuntimeError("не заданы TG_API_ID и TG_API_HASH")
    # Имя устройства хозяин видит в письме о входе и в «Устройствах»:
    # без него там случайная строка, и вход выглядит как взлом.
    return TelegramClient(str(SESSION_DIR / loop_session), api_id, api_hash,
                          device_model="Помощник Вершины",
                          system_version="Сервер", app_version="1.0")


def tg_send_code(phone: str) -> dict[str, Any]:
    async def run():
        client = _tg_client()
        await client.connect()
        try:
            if await client.is_user_authorized():
                me = await client.get_me()
                return {"ok": True, "already": True,
                        "name": getattr(me, "first_name", "")}
            sent = await client.send_code_request(phone)
            return {"ok": True, "hash": sent.phone_code_hash}
        finally:
            await client.disconnect()
    try:
        res = asyncio.run(run())
    except Exception as exc:
        return {"ok": False, "error": _tg_error(exc)}
    if res.get("hash"):
        put("tg_phone", phone)
        put("tg_hash", res["hash"])
    return res


def tg_sign_in(code: str, password: str = "") -> dict[str, Any]:
    phone = get("tg_phone", "") or ""
    phone_hash = get("tg_hash", "") or ""

    async def run():
        from telethon.errors import SessionPasswordNeededError
        client = _tg_client()
        await client.connect()
        try:
            try:
                await client.sign_in(phone, code.strip(),
                                     phone_code_hash=phone_hash)
            except SessionPasswordNeededError:
                if not password:
                    return {"ok": False, "need_password": True}
                await client.sign_in(password=password)
            me = await client.get_me()
            return {"ok": True, "name": getattr(me, "first_name", ""),
                    "username": getattr(me, "username", None)}
        finally:
            await client.disconnect()
    try:
        return asyncio.run(run())
    except Exception as exc:
        return {"ok": False, "error": _tg_error(exc)}


def _tg_error(exc: Exception) -> str:
    """Ошибки телеграма — человеческим языком."""
    name = type(exc).__name__
    table = {
        "PhoneNumberInvalidError": "Телеграм не знает такого номера. "
                                   "Введите его с кодом страны, например +7…",
        "PhoneCodeInvalidError": "Код не подошёл. Проверьте и наберите заново.",
        "PhoneCodeExpiredError": "Код устарел. Запросите новый.",
        "PasswordHashInvalidError": "Пароль не подошёл.",
        "FloodWaitError": "Телеграм просит подождать — слишком много попыток. "
                          "Повторите через несколько минут.",
    }
    if name in table:
        return table[name]
    return f"Телеграм ответил ошибкой ({name}). Попробуйте ещё раз."


def tg_ready() -> bool:
    """Есть ли рабочая сессия телеграма."""
    async def run():
        client = _tg_client()
        await client.connect()
        try:
            return await client.is_user_authorized()
        finally:
            await client.disconnect()
    try:
        return bool(asyncio.run(run()))
    except Exception:
        return False


# --------------------------------------------------------------- claude

def claude_status() -> dict[str, Any]:
    """Спрашивает у Claude Code, вошли ли мы. Отдаёт разобранный JSON."""
    binary = os.environ.get("VERSHINA_CLAUDE_BIN", "claude")
    # Токен из мастера живёт в bot.env, а не в окружении веба: без него
    # статус врёт «не вошли», хотя бот уже работает.
    env = os.environ.copy()
    token = read_env(BOT_ENV).get("CLAUDE_CODE_OAUTH_TOKEN")
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    try:
        done = subprocess.run([binary, "auth", "status"], env=env,
                              capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return {"loggedIn": False, "error": "Claude Code не установлен"}
    except subprocess.TimeoutExpired:
        return {"loggedIn": False, "error": "Claude Code не ответил"}
    try:
        return json.loads(done.stdout or "{}")
    except json.JSONDecodeError:
        return {"loggedIn": False, "error": (done.stdout or done.stderr or "").strip()[:200]}


def claude_ready() -> bool:
    return bool(claude_status().get("loggedIn"))


# Живые входы в Claude: ключ шага → процесс в псевдотерминале.
_logins: dict[str, dict[str, Any]] = {}
# Ссылка приходит вперемешку с управляющими кодами рисования экрана,
# поэтому обрываем её на первом же таком коде.
_URL = re.compile(r"https://[^\s\x1b\x07]+")
# setup-token не сохраняет вход, а печатает токен — его надо забрать самим.
_TOKEN = re.compile(r"sk-ant-oat[0-9a-z]*-[A-Za-z0-9_\-]{20,}")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")


def claude_login_start() -> dict[str, Any]:
    """Запускает вход и вылавливает ссылку, которую нужно открыть.

    Claude Code рассчитан на терминал, поэтому запускаем его в
    псевдотерминале и разбираем вывод. Место хрупкое: если CLI поменяет
    вывод, ссылку мы не найдём — тогда остаётся запасной путь со вставкой
    готового токена, см. claude_save_token.
    """
    import fcntl
    import pty
    import select
    import struct
    import termios

    binary = os.environ.get("VERSHINA_CLAUDE_BIN", "claude")
    # Повторное нажатие не должно менять ссылку: хозяин мог уже открыть
    # прежнюю, и код с неё к новому процессу не подойдёт.
    live = _logins.get("current")
    if live and live.get("url") and time.time() - live.get("started", 0) < 600:
        try:
            if os.waitpid(live["pid"], os.WNOHANG) == (0, 0):
                return {"ok": True, "url": live["url"]}
        except ChildProcessError:
            pass
    claude_login_cancel()
    try:
        pid, fd = pty.fork()
    except OSError as exc:
        return {"ok": False, "error": f"не удалось запустить вход: {exc}"}
    if pid == 0:                                   # дочерний процесс
        os.environ["TERM"] = "xterm-256color"
        os.execvp(binary, [binary, "setup-token"])
        os._exit(1)

    # Размеры окна обязательны: без них консольное приложение не выводит
    # ничего вообще и вход молча зависает. Проверено на живом CLI.
    try:
        # Окно широкое, чтобы токен в конце не разорвало переносом строки.
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 500, 0, 0))
    except OSError:
        pass

    _logins["current"] = {"pid": pid, "fd": fd, "buf": ""}
    deadline = time.time() + 40
    buf = ""
    while time.time() < deadline:
        ready, _, _ = select.select([fd], [], [], 1.0)
        if not ready:
            continue
        try:
            chunk = os.read(fd, 8192).decode("utf-8", "replace")
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        found = _URL.search(buf)
        if found:
            url = found.group(0).rstrip(".,)")
            _logins["current"].update(buf=buf, url=url, started=time.time())
            return {"ok": True, "url": url}
    claude_login_cancel()
    return {"ok": False, "error":
            "Не удалось получить ссылку для входа. Воспользуйтесь запасным "
            "способом ниже.", "output": buf[-400:]}


def claude_login_finish(code: str) -> dict[str, Any]:
    """Отдаёт вставленный код в ожидающий процесс и проверяет результат."""
    live = _logins.get("current")
    if not live:
        return {"ok": False, "error": "Вход не начат — нажмите «Войти» заново."}
    try:
        # Enter для консольного интерфейса — это \r, а не \n: с \n код
        # просто остаётся в поле ввода. Проверено на живом CLI.
        os.write(live["fd"], code.strip().encode())
        time.sleep(0.5)
        os.write(live["fd"], b"\r")
    except OSError as exc:
        return {"ok": False, "error": f"не удалось передать код: {exc}"}
    import select

    fd = live["fd"]
    buf = ""
    deadline = time.time() + 60
    while time.time() < deadline:
        ready, _, _ = select.select([fd], [], [], 2.0)
        if ready:
            try:
                chunk = os.read(fd, 8192).decode("utf-8", "replace")
            except OSError:
                chunk = ""
            buf += chunk
            clean = _ANSI.sub("", buf)
            found = _TOKEN.search(clean)
            if not found and "OAuth error" in clean:
                claude_login_cancel()
                return {"ok": False, "error":
                        "Claude не принял код. Нажмите «Войти в Claude», откройте "
                        "новую ссылку и скопируйте код именно с неё."}
            if found:
                claude_save_token(found.group(0))
                claude_login_cancel()
                if claude_ready():
                    return {"ok": True, "status": claude_status()}
                break
            if not chunk:
                break
        elif claude_ready():
            claude_login_cancel()
            return {"ok": True, "status": claude_status()}
    claude_login_cancel()
    return {"ok": False, "error":
            "Вход не подтвердился. Проверьте код или воспользуйтесь "
            "запасным способом."}


def claude_login_cancel() -> None:
    live = _logins.pop("current", None)
    if not live:
        return
    try:
        os.close(live["fd"])
    except OSError:
        pass
    try:
        os.kill(live["pid"], 9)
        os.waitpid(live["pid"], os.WNOHANG)
    except OSError:
        pass


def claude_save_token(value: str) -> dict[str, Any]:
    """Запасной путь: готовый токен подписки или ключ API вставкой.

    Ключ API (sk-ant-…) кладём в настройки бота — по нему помощник работает
    с оплатой за обращения. Долгоживущий токен подписки кладём туда же,
    Claude Code читает его из окружения.
    """
    value = value.strip()
    if not value:
        return {"ok": False, "error": "Пустое поле."}
    # Токен подписки тоже начинается с sk-ant- (sk-ant-oat01-…), поэтому
    # отличаем его первым — иначе он уезжает в ключ API и не работает.
    if value.startswith("sk-ant-") and not value.startswith("sk-ant-oat"):
        write_env(BOT_ENV, {"ANTHROPIC_API_KEY": value})
        return {"ok": True, "kind": "ключ API"}
    write_env(BOT_ENV, {"CLAUDE_CODE_OAUTH_TOKEN": value})
    return {"ok": True, "kind": "токен подписки"}


# ------------------------------------------------------------- готовность

def state() -> dict[str, Any]:
    """Что уже сделано — для галочек в мастере."""
    env = read_env(BOT_ENV)
    conn = _conn()
    try:
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    except Exception:
        users = 0
    finally:
        conn.close()
    return {
        "account": users > 0,
        "bot": bool(env.get("VERSHINA_BOT_TOKEN")),
        "bot_username": get("bot_username") or "",
        "owner": bool(env.get("VERSHINA_BOT_ALLOW")),
        "telegram": tg_ready(),
        "claude": claude_ready() or bool(env.get("ANTHROPIC_API_KEY")
                                         or env.get("CLAUDE_CODE_OAUTH_TOKEN")),
        "step": current_step(),
        "done": is_done(),
    }


def finish() -> None:
    """Закрывает настройку и гасит одноразовую ссылку."""
    global _done_cache
    put("done", "1")
    _done_cache = True
    put("step", "check")
    conn = _conn()
    try:
        conn.execute("DELETE FROM settings WHERE key=?", ("setup:token",))
        conn.execute("DELETE FROM settings WHERE key=?", ("setup:tg_hash",))
        conn.commit()
    finally:
        conn.close()
