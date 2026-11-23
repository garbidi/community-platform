"""Телеграм-аккаунт сообщества: вход со страницы платформы.

Аккаунт ассистента Вершины читает чаты потока и чат с чеками. Вход —
обычный телеграмный: номер → код в приложении → облачный пароль, если
он есть. Код человек вводит на странице сам: пересланный в телеграме
код телеграм замечает и гасит.

Логика входа — из мастера помощника (app/setup.py), отлаженная вживую.
Клиент между запросами не держим: каждый шаг открывает свой поверх одного
файла сессии, phone_code_hash лежит в login.json — вход переживает
перезапуск приложения. Сессия — /data/tg/community.session; это ключ
к аккаунту, наружу не отдаётся.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

TG_DIR = Path(os.environ.get("VERSHINA_TG_DIR", "/data/tg"))
SESSION = TG_DIR / "community"
LOGIN = TG_DIR / "login.json"
CHATS = TG_DIR / "chats.json"


def configured() -> bool:
    return bool(os.environ.get("TG_API_ID") and os.environ.get("TG_API_HASH"))


def _client():
    from telethon import TelegramClient
    TG_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(TG_DIR, 0o700)
    # Имя устройства владелец видит в «Устройствах» и в письме о входе:
    # без него там случайная строка, и вход выглядит как взлом.
    return TelegramClient(str(SESSION), int(os.environ["TG_API_ID"]),
                          os.environ["TG_API_HASH"],
                          device_model="Платформа Вершины",
                          system_version="Сервер", app_version="1.0")


# Один разговор с телеграмом за раз: файл сессии общий, а страница чатов
# просит картинки и аватарки пачками — параллельные подключения его ломают.
_LOCK = __import__("threading").RLock()


TEST_CHATS_DEFAULT = "-5000000003"     # группа «Бот»: Станислав, Марья, аккаунт сообщества


def send_mode() -> tuple[str, set[int]]:
    """Положение замка: off — закрыт; test — только тестовые чаты; on — везде.
    Открывает администратор в «Управление» → «Телеграм»."""
    from . import store
    conn = store.connect()
    try:
        mode = store.get_setting(conn, "tg:send", "off")
        raw = store.get_setting(conn, "tg:test_chats", TEST_CHATS_DEFAULT) or ""
    finally:
        conn.close()
    chats = {int(x) for x in raw.replace(",", " ").split() if x.lstrip("-").isdigit()}
    return (mode if mode in ("off", "test", "on") else "off"), chats


def sending_allowed(chat_id: int | None = None) -> bool:
    """Замок на всё, что уходит в телеграм (сообщения, удаление). По умолчанию
    закрыт. В режиме test открыт только для тестовых чатов (сейчас — «Бот»)."""
    mode, chats = send_mode()
    if mode == "on":
        return True
    return mode == "test" and chat_id is not None and int(chat_id) in chats


LOCKED = {"ok": False, "error": "Отправка в телеграм выключена. Включает администратор: «Управление» → «Телеграм»."}


def _run(coro_fn) -> dict[str, Any]:
    from .proclock import held
    # файл сессии один на все процессы платформы — по очереди
    with _LOCK, held("telegram"):
        return _run_unlocked(coro_fn)


def _run_unlocked(coro_fn) -> dict[str, Any]:
    async def wrapped():
        client = _client()
        await client.connect()
        try:
            return await coro_fn(client)
        finally:
            await client.disconnect()
            # файл сессии — ключ к аккаунту: читать его может только владелец
            f = SESSION.with_suffix(".session")
            if f.exists():
                os.chmod(f, 0o600)
    try:
        return asyncio.run(wrapped())
    except Exception as exc:
        return {"ok": False, "error": _error(exc)}


def _error(exc: Exception) -> str:
    name = type(exc).__name__
    table = {
        "PhoneNumberInvalidError": "Телеграм не знает такого номера. "
                                   "Введите его с кодом страны, например +7…",
        "PhoneCodeInvalidError": "Код не подошёл. Проверьте и наберите заново.",
        "PhoneCodeExpiredError": "Код устарел или был переслан в телеграме — "
                                 "так телеграм его гасит. Запросите новый и "
                                 "введите здесь, не пересылая.",
        "PasswordHashInvalidError": "Облачный пароль не подошёл.",
        "FloodWaitError": "Телеграм просит подождать — слишком много попыток. "
                          "Повторите через несколько минут.",
        "PhoneNumberBannedError": "Этот номер заблокирован в телеграме.",
    }
    return table.get(name, f"Телеграм ответил ошибкой ({name}). Попробуйте ещё раз.")


def _me(me) -> dict[str, Any]:
    name = " ".join(x for x in (getattr(me, "first_name", ""), getattr(me, "last_name", "")) if x)
    return {"name": name, "username": getattr(me, "username", None),
            "phone": getattr(me, "phone", None)}


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    TG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def status() -> dict[str, Any]:
    if not configured():
        return {"ok": False, "error": "На сервере не заданы ключи TG_API_ID и TG_API_HASH."}

    async def run(client):
        if not await client.is_user_authorized():
            return {"ok": True, "connected": False}
        return {"ok": True, "connected": True, **_me(await client.get_me())}
    res = _run(run)
    login = _read_json(LOGIN)
    res["waiting_code"] = bool(login.get("hash")) and not res.get("connected")
    res["need_password"] = res["waiting_code"] and bool(login.get("code_ok"))
    res["phone_sent"] = login.get("phone")
    return res


def send_code(phone: str) -> dict[str, Any]:
    phone = "".join(ch for ch in phone if ch.isdigit() or ch == "+")
    if not phone.startswith("+"):
        phone = "+" + phone

    async def run(client):
        if await client.is_user_authorized():
            return {"ok": True, "already": True}
        sent = await client.send_code_request(phone)
        return {"ok": True, "hash": sent.phone_code_hash}
    res = _run(run)
    if res.get("hash"):
        _write_json(LOGIN, {"phone": phone, "hash": res.pop("hash"),
                            "at": datetime.now().isoformat(timespec="seconds")})
    return res


def sign_in(code: str, password: str = "") -> dict[str, Any]:
    login = _read_json(LOGIN)
    if not login.get("hash"):
        return {"ok": False, "error": "Сначала запросите код."}
    code = "".join(ch for ch in code if ch.isdigit())

    async def run(client):
        from telethon.errors import SessionPasswordNeededError
        if not await client.is_user_authorized():
            # Код уже принят, ждали только пароль: второй раз код не шлём,
            # иначе телеграм ответит «код неверный».
            need_pw = bool(login.get("code_ok"))
            if not need_pw:
                try:
                    await client.sign_in(login["phone"], code, phone_code_hash=login["hash"])
                except SessionPasswordNeededError:
                    need_pw = True
            if need_pw:
                if not password:
                    return {"ok": False, "need_password": True}
                await client.sign_in(password=password)
        return {"ok": True, **_me(await client.get_me())}
    res = _run(run)
    if res.get("need_password"):
        _write_json(LOGIN, {**login, "code_ok": True})
    if res.get("ok"):
        LOGIN.unlink(missing_ok=True)
        refresh_chats()
    return res


def refresh_chats() -> dict[str, Any]:
    """Список групп и каналов аккаунта — проверить, добавлен ли он куда надо."""
    async def run(client):
        if not await client.is_user_authorized():
            return {"ok": False, "error": "Аккаунт не подключён."}
        from telethon import utils
        from telethon.tl.types import PeerChannel
        chats, migrated = [], {}
        async for d in client.iter_dialogs(limit=1000):
            if not (d.is_group or d.is_channel):
                continue
            ent = d.entity
            # Обычную группу телеграм превращает в супергруппу, а старая пустая
            # копия остаётся в списке с тем же названием — её не показываем,
            # а запоминаем, куда она переехала (роль переносится на новую)
            moved = getattr(ent, "migrated_to", None)
            if moved is not None and getattr(moved, "channel_id", None):
                migrated[d.id] = utils.get_peer_id(PeerChannel(moved.channel_id))
                continue
            if getattr(ent, "deactivated", False):
                continue
            item = {
                "id": d.id, "title": d.name,
                "kind": "канал" if d.is_channel and not d.is_group else "группа",
                "members": getattr(ent, "participants_count", None),
                "last": d.date.isoformat() if d.date else None,
            }
            if getattr(ent, "forum", False):
                # группа с темами: у каждой темы своя лента — роли задаются по темам
                item["topics"] = await _topics(client, ent)
            chats.append(item)
        return {"ok": True, "chats": chats, "migrated": migrated}
    res = _run(run)
    if res.get("ok"):
        _write_json(CHATS, {"at": datetime.now().isoformat(timespec="seconds"),
                            "chats": res["chats"], "migrated": res["migrated"]})
    return res


async def _topics(client, ent) -> list[dict[str, Any]]:
    """Темы группы-форума. «Общий» (id 1) есть всегда, даже если скрыт."""
    from telethon.tl import functions
    req = getattr(functions.messages, "GetForumTopicsRequest", None) \
        or getattr(functions.channels, "GetForumTopicsRequest")
    out, offset_id, offset_topic, offset_date = [], 0, 0, None
    try:
        for _ in range(10):                       # до 1000 тем — с запасом
            peer = await client.get_input_entity(ent)
            res = await client(req(peer, offset_date, offset_id, offset_topic, 100))
            got = [t for t in res.topics if getattr(t, "title", None)]
            for t in got:
                out.append({"id": t.id, "title": t.title,
                            "closed": bool(getattr(t, "closed", False)),
                            "hidden": bool(getattr(t, "hidden", False))})
            if len(res.topics) < 100:
                break
            last = res.topics[-1]
            offset_topic, offset_id = last.id, getattr(last, "top_message", 0)
            offset_date = getattr(last, "date", None)
    except Exception:                               # noqa: BLE001 — нет тем, так нет
        return out
    if not any(t["id"] == 1 for t in out):
        out.insert(0, {"id": 1, "title": "Общий", "closed": False, "hidden": True})
    out.sort(key=lambda t: (t["id"] != 1, t["title"].lower()))
    return out


def chats() -> dict:
    return _read_json(CHATS)


def read_messages(chat_id: int, since: datetime | None, limit: int = 3000,
                  before_id: int = 0) -> dict[str, Any]:
    """Сообщения чата с момента since (или limit штук старше before_id —
    подгрузка истории) — только чтение: не отмечает прочитанным, ничего
    не пишет и не ставит реакций."""
    async def run(client):
        if not await client.is_user_authorized():
            return {"ok": False, "error": "Аккаунт сообщества не подключён."}
        ent = await client.get_entity(chat_id)
        out = []
        async for m in client.iter_messages(ent, limit=limit, offset_id=before_id):
            if since and m.date < since:
                break
            if getattr(m, "action", None):          # служебные: вступил, закрепил…
                continue
            s = m.sender
            name = " ".join(x for x in (getattr(s, "first_name", None), getattr(s, "last_name", None)) if x) \
                or getattr(s, "title", None) or ""
            media = ("кружок" if m.video_note else "голос" if m.voice else "фото" if m.photo
                     else "видео" if m.video else "файл" if m.document else None)
            # премиум-эмодзи: где в тексте стоят и какая картинка (смещения — в UTF-16, как в телеграме)
            emojis = [[e.offset, e.length, e.document_id] for e in (m.entities or [])
                      if e.__class__.__name__ == "MessageEntityCustomEmoji"]
            rt = m.reply_to
            out.append({"id": m.id, "date": m.date.isoformat(), "sender_id": m.sender_id,
                        "username": getattr(s, "username", None), "name": name,
                        "text": m.raw_text or "", "media": media, "reply": bool(rt),
                        "emojis": emojis,
                        # на что ответ (в группе с темами reply_to — это ещё и тема)
                        "reply_to": getattr(rt, "reply_to_msg_id", None) if rt else None,
                        "topic": (getattr(rt, "reply_to_top_id", None) or
                                  (getattr(rt, "reply_to_msg_id", None) if getattr(rt, "forum_topic", False) else None))
                                 if rt else None,
                        "album": m.grouped_id, "out": bool(m.out), "fwd": bool(m.fwd_from)})
        return {"ok": True, "messages": out}
    return _run(run)


def search_messages(chat_id: int, query: str, limit: int = 40) -> dict[str, Any]:
    """Поиск по чату на стороне телеграма (свежие сверху) — только чтение."""
    async def run(client):
        if not await client.is_user_authorized():
            return {"ok": False, "error": "Аккаунт сообщества не подключён."}
        ent = await client.get_entity(chat_id)
        out = []
        async for m in client.iter_messages(ent, search=query, limit=limit):
            out.append({"id": m.id, "date": m.date.isoformat(), "text": m.raw_text or "", "sender_id": m.sender_id})
        return {"ok": True, "messages": out}
    return _run(run)


def fetch_media(chat_id: int, msg_ids: list[int], folder) -> dict[str, Any]:
    """Картинки сообщений для просмотра на платформе: фото — уменьшенная копия,
    видео и кружки — кадр-превью. Только чтение. Файл: folder/<чат>_<сообщение>.jpg"""
    import os

    async def run(client):
        ent = await client.get_entity(chat_id)
        got = 0
        for m in await client.get_messages(ent, ids=msg_ids):
            if not m:
                continue
            path = f"{folder}/{chat_id}_{m.id}.jpg"
            if os.path.exists(path):
                continue
            try:
                if m.photo:
                    await client.download_media(m, file=path, thumb=-1)
                elif m.video or m.video_note or m.document and getattr(m.document, "thumbs", None):
                    await client.download_media(m, file=path, thumb=-1)
                else:
                    continue
                got += 1
            except Exception:                    # noqa: BLE001 — нет превью, так нет
                continue
        return {"ok": True, "got": got}
    return _run(run)


def fetch_avatars(user_ids: list[int], folder) -> dict[str, Any]:
    """Аватарки людей (маленькие) — для чатов на платформе и фото к «Сенсационным
    новостям». Только чтение. Файл: folder/<id>.jpg; нет аватарки — нет файла."""
    import os

    async def run(client):
        got = 0
        for uid in user_ids:
            path = f"{folder}/{uid}.jpg"
            if os.path.exists(path) or os.path.exists(path + ".none"):
                continue
            try:
                r = await client.download_profile_photo(uid, file=path, download_big=False)
                if not r:
                    open(path + ".none", "w").close()      # аватарки нет — не спрашивать снова
                else:
                    got += 1
            except Exception:                    # noqa: BLE001 — нет доступа к аватарке
                open(path + ".none", "w").close()
        return {"ok": True, "got": got}
    return _run(run)


def fetch_avatar_big(user_id: int, path: str) -> bool:
    """Аватарка в полном размере — для фото к посту о вовлечении."""
    async def run(client):
        r = await client.download_profile_photo(user_id, file=path, download_big=True)
        return {"ok": bool(r)}
    return bool(_run(run).get("ok"))


def emoji_docs(ids: list[int], folder) -> dict[int, dict]:
    """Описание премиум-эмодзи по номерам: символ-заменитель, набор, картинка
    для предпросмотра (сохраняется в folder/<id>.thumb). Только чтение."""
    async def run(client):
        from telethon.tl.functions.messages import GetCustomEmojiDocumentsRequest
        out = {}
        for i in range(0, len(ids), 100):
            for d in await client(GetCustomEmojiDocumentsRequest(document_id=ids[i:i + 100])):
                a = next((a for a in d.attributes if a.__class__.__name__ == "DocumentAttributeCustomEmoji"), None)
                thumb = f"{folder}/{d.id}.thumb"
                if d.thumbs and not __import__("os").path.exists(thumb):
                    await client.download_media(d, file=thumb, thumb=-1)
                out[d.id] = {"alt": a.alt if a else "", "set": getattr(getattr(a, "stickerset", None), "id", None)}
        return {"ok": True, "docs": out}
    return _run(run)


def send(chat_id: int, text: str, topic_id: int = 0, files: list[str] | None = None,
         reply_to: int | None = None) -> dict[str, Any]:
    """Написать в чат от имени аккаунта. Простым текстом: без разметки,
    чтобы «_» в никах и «*» в тексте ушли как есть. topic_id — тема группы
    с темами; 0 и 1 («Общий») — без темы, в общую ленту."""
    if not sending_allowed(chat_id):
        return dict(LOCKED)

    async def run(client):
        if not await client.is_user_authorized():
            return {"ok": False, "error": "Аккаунт сообщества не подключён."}
        ent = await client.get_entity(chat_id)
        from . import emoji as _emoji
        plain, ents = _emoji.render((text or "").replace("\r\n", "\n").replace("\r", "\n"))              # [[СЛОВО]] и значки → премиум-эмодзи Марьи
        target = reply_to or (topic_id if topic_id > 1 else None)
        if files:
            # несколько картинок — одним альбомом, текст — подписью к нему
            m = await client.send_file(ent, files if len(files) > 1 else files[0], caption=plain or None,
                                       parse_mode=None, formatting_entities=ents or None, reply_to=target)
            m = m[0] if isinstance(m, list) else m
        else:
            m = await client.send_message(ent, plain, parse_mode=None, link_preview=False,
                                          formatting_entities=ents or None, reply_to=target)
        return {"ok": True, "msg_id": m.id}
    return _run(run)


def migrated_to(chat_id: int) -> int | None:
    """Обычную группу телеграм превращает в супергруппу (смена настроек, админы) — у неё
    новый номер, старая замирает. Вернуть новый номер или None, если переезда не было."""
    if str(chat_id).startswith("-100") or chat_id > 0:
        return None

    async def run(client):
        from telethon import utils
        from telethon.tl.types import PeerChannel
        ent = await client.get_entity(chat_id)
        moved = getattr(ent, "migrated_to", None)
        if moved is not None and getattr(moved, "channel_id", None):
            return {"ok": True, "to": utils.get_peer_id(PeerChannel(moved.channel_id))}
        return {"ok": True, "to": None}
    return _run(run).get("to")


def set_chat_photo(chat_id: int, path: str) -> dict[str, Any]:
    """Поставить фото группы (счётчик «вовлекли N»). Тот же замок, что и на отправку."""
    if not sending_allowed(chat_id):
        return dict(LOCKED)

    async def run(client):
        if not await client.is_user_authorized():
            return {"ok": False, "error": "Аккаунт сообщества не подключён."}
        from telethon import functions, types
        ent = await client.get_entity(chat_id)
        up = await client.upload_file(path)
        photo = types.InputChatUploadedPhoto(file=up)
        if isinstance(ent, types.Channel):          # супергруппа
            await client(functions.channels.EditPhotoRequest(channel=ent, photo=photo))
        else:                                       # обычная группа
            await client(functions.messages.EditChatPhotoRequest(chat_id=ent.id, photo=photo))
        return {"ok": True}
    return _run(run)


def unsend(chat_id: int, msg_id: int) -> dict[str, Any]:
    """Удалить своё сообщение из чата у всех."""
    if not sending_allowed(chat_id):
        return dict(LOCKED)

    async def run(client):
        if not await client.is_user_authorized():
            return {"ok": False, "error": "Аккаунт сообщества не подключён."}
        ent = await client.get_entity(chat_id)
        res = await client.delete_messages(ent, [msg_id], revoke=True)
        if not any(getattr(r, "pts_count", 0) for r in res):
            return {"ok": False, "error": "Телеграм не удалил сообщение — возможно, его уже нет."}
        return {"ok": True}
    return _run(run)


def logout() -> dict[str, Any]:
    async def run(client):
        if await client.is_user_authorized():
            await client.log_out()
        return {"ok": True}
    res = _run(run)
    for p in (LOGIN, CHATS, SESSION.with_suffix(".session")):
        p.unlink(missing_ok=True)
    return res
