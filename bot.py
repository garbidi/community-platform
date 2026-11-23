#!/usr/bin/env python3
"""Телеграм-бот помощника: тот же мозг, что у ask.py, только в чате.

Запуск:
    export VERSHINA_BOT_TOKEN=...      # токен от @BotFather
    export VERSHINA_BOT_ALLOW=12345    # кому отвечать, через запятую
    ./bot.sh start

Пока список разрешённых пуст, бот никому не отвечает по существу: он только
сообщает пришедшему его собственный id, чтобы этот id можно было вписать.
Данные сообщества закрытые — открытый бот здесь недопустим.
"""

from __future__ import annotations

import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import traceback
from collections import OrderedDict, defaultdict, deque
from pathlib import Path

import httpx

from app import brain, jobs, speech, store

TOKEN = os.environ.get("VERSHINA_BOT_TOKEN", "")
API = f"https://api.telegram.org/bot{TOKEN}"
ALLOW = {int(x) for x in os.environ.get("VERSHINA_BOT_ALLOW", "").replace(" ", "").split(",") if x}
TRACE = os.environ.get("VERSHINA_BOT_TRACE", "0") != "0"
CHUNK = 3800
HISTORY = 8                     # сколько реплик диалога помнить на чат

# Сколько вопросов держать в очереди на один чат, пока думаем над текущим.
QUEUE_MAX = int(os.environ.get("VERSHINA_BOT_QUEUE", 5))
# Через сколько секунд молчания показать, что работа идёт, и как часто
# обновлять эту строку. Телеграм не любит частых правок сообщения.
PROGRESS_AFTER = float(os.environ.get("VERSHINA_BOT_PROGRESS_AFTER", 8))
PROGRESS_EVERY = float(os.environ.get("VERSHINA_BOT_PROGRESS_EVERY", 4))

history: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY))

# Тексты ответов для кнопки «Прослушать». Живут только в памяти процесса:
# ответы бывают про личные переписки, и складывать их на диск незачем.
SPOKEN_KEEP = 100
spoken: "OrderedDict[tuple[int, int], str]" = OrderedDict()


def remember(chat_id: int, message_id: int | None, text: str) -> None:
    if not message_id:
        return
    spoken[(chat_id, message_id)] = text
    while len(spoken) > SPOKEN_KEEP:
        spoken.popitem(last=False)

def _help_text() -> str:
    """Справка под конкретную установку.

    Раздел про сообщество показываем, только если база и правда есть:
    у другого хозяина бот — личный помощник, и рассказывать ему про
    «Вершину» незачем.
    """
    base = """Я твой помощник. Спрашивай обычными словами:

· посмотри, о чём мы говорили с таким-то
· найди в чатах, что писали про оплату
· разбери переписку за месяц и скажи, что важного
· напомни, о чём мы договорились в августе

Голосом тоже можно — расшифрую и отвечу голосом, если попросишь.

Быстрый вопрос — отвечу за полминуты. Большой разбор возьму в работу
и пришлю отдельным сообщением: ждать не нужно, можно писать дальше.

Команды под кнопкой «/» слева от поля ввода. Из полезного:
/stop — бросить то, что считаю сейчас
/reset — забыть разговор и начать заново
/model — переключить на умную модель для тяжёлого разбора
/settings — что ты мне поручил помнить, и как вернуть назад"""

    try:
        if Path(os.environ.get("VERSHINA_KB_DB", "")).exists():
            base += """

Отдельно есть данные сообщества «Вершина»: участники, вовлечения,
расшифровки. Спрашивай про людей по имени, или /svodka — общая картина,
/quiet — кто затих."""
    except OSError:
        pass
    return base



def log(*args) -> None:
    print(time.strftime("%d.%m.%Y %H:%M:%S"), *args, flush=True)


def api(method: str, **data):
    for attempt in range(3):
        try:
            r = httpx.post(f"{API}/{method}", json=data, timeout=90)
            out = r.json()
            if not out.get("ok"):
                log("telegram отказал:", method, out.get("description"))
            return out
        except httpx.HTTPError as exc:
            log(f"сеть ({method}, попытка {attempt + 1}):", exc)
            time.sleep(2 + attempt * 3)
    return {"ok": False}


def download(file_id: str) -> str | None:
    """Забрать присланный файл во временный каталог."""
    info = api("getFile", file_id=file_id)
    path = (info.get("result") or {}).get("file_path")
    if not path:
        return None
    work = tempfile.mkdtemp(prefix="vershina-in-")
    dest = os.path.join(work, os.path.basename(path))
    try:
        with httpx.stream("GET", f"https://api.telegram.org/file/bot{TOKEN}/{path}",
                          timeout=120) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
    except httpx.HTTPError as exc:
        log("не скачался файл:", exc)
        return None
    return dest


def send_voice(chat_id: int, ogg: str, reply_to: int | None = None) -> None:
    data = {"chat_id": chat_id}
    if reply_to:
        data["reply_to_message_id"] = reply_to
    with open(ogg, "rb") as f:
        try:
            httpx.post(f"{API}/sendVoice", data=data,
                       files={"voice": ("answer.ogg", f, "audio/ogg")}, timeout=120)
        except httpx.HTTPError as exc:
            log("голосовое не ушло:", exc)


LISTEN = {"inline_keyboard": [[{"text": "🎧 Прослушать", "callback_data": "voice"}]]}


def send(chat_id: int, text: str, listen: bool = False) -> int | None:
    """Отправить ответ. Кнопка «Прослушать» вешается на последний кусок."""
    text = text.strip() or "—"
    last_id = None
    while text:
        part, text = text[:CHUNK], text[CHUNK:]
        if text:                                  # рвём по строке, а не по слову
            cut = part.rfind("\n")
            if cut > CHUNK // 2:
                text, part = part[cut:] + text, part[:cut]
        extra = {"reply_markup": LISTEN} if listen and not text else {}
        out = api("sendMessage", chat_id=chat_id, text=part,
                  disable_web_page_preview=True, **extra)
        last_id = ((out.get("result") or {}).get("message_id")) or last_id
    return last_id


class Typing:
    """Индикатор «печатает» на всё время ответа.

    Телеграм показывает его секунд пять, а ядро думает полминуты: без
    подновления собеседнику кажется, что бот умер.
    """

    def __init__(self, chat_id: int) -> None:
        self.chat_id = chat_id
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        while not self.stop.is_set():
            api("sendChatAction", chat_id=self.chat_id, action="typing")
            self.stop.wait(4)

    def __enter__(self) -> "Typing":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop.set()


def offset_get(conn) -> int:
    return int(store.get_setting(conn, "bot_offset", "0") or 0)


def offset_set(conn, value: int) -> None:
    store.set_setting(conn, "bot_offset", str(value))


def hear(chat_id: int, msg: dict) -> str:
    """Голосовое, кружок или аудио → текст вопроса."""
    media = msg.get("voice") or msg.get("video_note") or msg.get("audio")
    if not media:
        return ""
    api("sendChatAction", chat_id=chat_id, action="typing")
    path = download(media["file_id"])
    if not path:
        return ""
    try:
        heard = speech.listen(path)
    except Exception as exc:
        log("расшифровка не удалась:", exc)
        heard = ""
    finally:
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)
    log(f"услышал: «{heard[:80]}»" if heard else "речь не распознана")
    return heard


def reply_voice(chat_id: int, text: str, reply_to: int | None = None) -> None:
    api("sendChatAction", chat_id=chat_id, action="record_voice")
    ogg = None
    try:
        ogg = speech.speak(text)
        if ogg:
            send_voice(chat_id, ogg, reply_to)
    except Exception as exc:
        log("синтез не удался:", exc)
    finally:
        speech.cleanup(ogg)


# --------------------------------------------------------------- модель

# Обычные вопросы отвечает модель подешевле, тяжёлые разборы — умная.
# Угадывать по тексту вопроса бесполезно: «что говорили про штрафы» — шесть
# слов, а работы на два десятка шагов. Поэтому выбор оставлен человеку.
MODELS = {
    "обычная": "claude-sonnet-5",
    "сонет": "claude-sonnet-5",
    "умная": "claude-opus-5",
    "опус": "claude-opus-5",
}
MODEL_RU = {"claude-sonnet-5": "обычная", "claude-opus-5": "умная"}


def chat_model(chat_id: int) -> str:
    conn = store.connect()
    try:
        return store.get_setting(conn, f"model:{chat_id}", "") or brain.MODEL
    finally:
        conn.close()


def set_chat_model(chat_id: int, model: str) -> None:
    conn = store.connect()
    try:
        store.set_setting(conn, f"model:{chat_id}", model)
        conn.commit()
    finally:
        conn.close()


# ------------------------------------------------- ход работы и очередь

class Progress:
    """Одна строка в чате, которая обновляется по ходу долгой работы.

    Раньше собеседник видел четыре минуты тишины и решал, что бот умер.
    Теперь он видит, чем помощник занят прямо сейчас. Строка появляется
    не сразу: на быстрых ответах она только мешала бы.
    """

    def __init__(self, chat_id: int) -> None:
        self.chat_id = chat_id
        self.message_id: int | None = None
        self.started = time.time()
        self.last = 0.0
        self.lock = threading.Lock()

    def step(self, number: int, what: str) -> None:
        now = time.time()
        with self.lock:
            if now - self.started < PROGRESS_AFTER:
                return
            if self.message_id is not None and now - self.last < PROGRESS_EVERY:
                return
            self.last = now
            text = f"{what[:1].upper()}{what[1:]}… ({number})"
            if self.message_id is None:
                out = api("sendMessage", chat_id=self.chat_id, text=text)
                self.message_id = ((out or {}).get("result") or {}).get("message_id")
            else:
                api("editMessageText", chat_id=self.chat_id,
                    message_id=self.message_id, text=text)

    def done(self) -> None:
        with self.lock:
            if self.message_id is None:
                return
            api("deleteMessage", chat_id=self.chat_id,
                message_id=self.message_id)
            self.message_id = None


# Очередь на каждый чат. Приём сообщений идёт в главном цикле и никогда не
# ждёт помощника: иначе бот глохнет на всё время долгой работы и не слышит
# ни новых вопросов, ни команды «забудь», ни кнопок.
_queues: "dict[int, queue.Queue]" = {}
_workers: dict[int, threading.Thread] = {}
_running: dict[int, object] = {}        # чат → процесс ядра, чтобы его прервать
_qlock = threading.Lock()


def dispatch(chat_id: int, user_id: int, name: str, text: str,
             by_voice: bool) -> None:
    with _qlock:
        q = _queues.get(chat_id)
        if q is None:
            q = _queues[chat_id] = queue.Queue(maxsize=QUEUE_MAX)
        try:
            q.put_nowait((user_id, name, text, by_voice))
        except queue.Full:
            send(chat_id, "Столько вопросов сразу я не удержу. "
                          "Отвечу на прежние — спроси это ещё раз.")
            return
        waiting = q.qsize()
        worker = _workers.get(chat_id)
        if worker is None or not worker.is_alive():
            worker = threading.Thread(target=_worker, args=(chat_id,),
                                      daemon=True)
            _workers[chat_id] = worker
            worker.start()
    if waiting > 1:
        send(chat_id, f"Принял, отвечу по очереди — передо мной ещё "
                      f"{waiting - 1}.")


def _worker(chat_id: int) -> None:
    """Живёт, пока жив бот: чатов у нас единицы, держать поток дёшево."""
    q = _queues[chat_id]
    while True:
        user_id, name, text, by_voice = q.get()
        try:
            _think(chat_id, user_id, name, text, by_voice)
        except Exception:
            log("ошибка обработки:\n" + traceback.format_exc())
            send(chat_id, "Не смог ответить: внутренняя ошибка. "
                          "Она записана в лог, спросите иначе или позже.")
        finally:
            with _qlock:
                _running.pop(chat_id, None)
            q.task_done()


# ------------------------------------------------------- долгие задачи

# Помощник не может отцепить работу изнутри своего хода: фоновые процессы
# гибнут вместе с ним. Поэтому он кладёт задачу в очередь и заканчивает ход,
# а выполняет её вот этот работник — отдельным ходом, со своей сессией,
# и сам присылает результат в чат.
JOB_POLL = float(os.environ.get("VERSHINA_BOT_JOB_POLL", 5))


def jobs_worker() -> None:
    jobs.init()
    back = jobs.revive()
    if back:
        log(f"вернул в очередь незаконченных задач: {back}")
    while True:
        try:
            job = jobs.take()
        except Exception:
            log("очередь задач недоступна:\n" + traceback.format_exc())
            time.sleep(30)
            continue
        if job is None:
            time.sleep(JOB_POLL)
            continue
        # Ни одна задача не должна убивать работника: упадёт он — встанут все.
        try:
            _run_job(job)
        except Exception:
            log(f"задача #{job['id']}: не смог обработать\n"
                + traceback.format_exc())
            try:
                jobs.finish(job["id"], False, "не смог обработать")
            except Exception:
                pass


def _run_job(job) -> None:
    """Выполняет одну отложенную задачу и присылает результат в чат."""
    try:
        chat_id = int(str(job["chat"]).strip())
    except ValueError:
        log(f"задача #{job['id']}: чат «{job['chat']}» не похож на адрес, пропускаю")
        jobs.finish(job["id"], False, "неизвестный адрес чата")
        return

    started = time.time()
    log(f"задача #{job['id']}: «{job['task'][:60]}» — начал")
    try:
        # Своя сессия на задачу: две работы в одной сессии мешают друг другу.
        res = brain.answer(job["task"], chat_key=f"job{job['id']}",
                           model=chat_model(chat_id))
    except Exception as exc:
        log(f"задача #{job['id']}: сбой\n" + traceback.format_exc())
        jobs.finish(job["id"], False, str(exc)[:500])
        send(chat_id, f"Не справился с задачей «{job['task'][:60]}». "
                      + brain._excuse(exc))
        return

    text = res.get("text") or "Пустой ответ."
    log(f"задача #{job['id']}: готово за {time.time() - started:.0f} c"
        + (f", ${res['cost']:.3f}" if res.get("cost") else ""))
    jobs.finish(job["id"], True, text)
    message_id = send(chat_id, f"Готово: {job['task'][:80]}\n\n{text}",
                      listen=speech.offered())
    remember(chat_id, message_id, text)


def handle(chat_id: int, user_id: int, name: str, text: str,
           by_voice: bool = False) -> None:
    if user_id not in ALLOW:
        log(f"отказ: {name} (id {user_id}) не в списке")
        send(chat_id, "Доступ к данным сообщества закрыт.\n"
                      f"Ваш телеграм-id: {user_id}. Передайте его тому, "
                      "кто настраивал помощника, — он добавит вас в список.")
        return

    low = text.lower().strip()
    if low in ("/start", "/help", "помощь"):
        send(chat_id, _help_text())
        return
    if low == "/id":
        send(chat_id, f"Ваш телеграм-id: {user_id}")
        return
    if low == "/reset":
        history.pop(chat_id, None)
        brain._forget(str(chat_id))
        send(chat_id, "Разговор забыт, начинаем с чистого листа.")
        return
    if low.startswith(("/модель", "/model")):
        want = low.split(maxsplit=1)[1].strip() if " " in low else ""
        if not want:
            now = chat_model(chat_id)
            send(chat_id,
                 f"Сейчас отвечаю: {MODEL_RU.get(now, now)}.\n\n"
                 "«/модель умная» — для тяжёлых разборов, отвечает лучше, "
                 "но расходует лимит втрое быстрее.\n"
                 "«/модель обычная» — на каждый день.")
            return
        if want not in MODELS:
            send(chat_id, "Не понял. Есть «обычная» и «умная».")
            return
        set_chat_model(chat_id, MODELS[want])
        send(chat_id, f"Переключился: {want}. "
                      "Следующий ответ будет чуть дороже обычного — "
                      "разговор начнётся с чистого листа для новой модели.")
        return
    if low.startswith(("/настройки", "/settings")):
        hist = brain.JARVIS_DIR / "history"
        entries = sorted(hist.glob("CLAUDE.md.*"), reverse=True)
        if low.endswith("сброс") or low.endswith("reset"):
            if not entries:
                send(chat_id, "Откатывать не к чему — я себя ещё не менял.")
                return
            shutil.copy2(entries[0], brain.JARVIS_DIR / "CLAUDE.md")
            when = entries[0].name.split(".")[-1]
            send(chat_id, f"Вернул инструкции к виду от {when[:8]}. "
                          "Новый разговор начнётся уже с ними.")
            log(f"откат инструкций к {entries[0].name}")
            return
        journal = hist / "journal.log"
        if not journal.exists():
            send(chat_id, "Я себя ни разу не менял — инструкции такие, "
                          "какими их написали при установке.")
            return
        lines = journal.read_text(encoding="utf-8").strip().splitlines()[-5:]
        send(chat_id, "Последние изменения:\n" + "\n".join(lines)
             + "\n\n«/настройки сброс» — вернуть как было до последней правки.")
        return
    if low in ("/задачи", "/jobs"):
        rows = jobs.waiting(str(chat_id))
        if not rows:
            send(chat_id, "Сейчас ничего не выполняется.")
        else:
            lines = [f"· {r['task'][:70]} — {r['state']}" for r in rows]
            send(chat_id, "В работе:\n" + "\n".join(lines))
        return
    if low in ("/stop", "/стоп"):
        with _qlock:
            proc = _running.get(chat_id)
        if proc is None:
            send(chat_id, "Сейчас я ничего не считаю.")
        else:
            brain._halt(proc)
            send(chat_id, "Остановил.")
        return
    if low in ("/svodka", "/сводка"):
        text = "дай общую сводку по потоку"
    elif low == "/quiet":
        text = "кто затих больше семи дней"

    # Всё остальное — в очередь: думать будем в отдельном потоке, а этот
    # вернётся забирать следующие сообщения.
    dispatch(chat_id, user_id, name, text, by_voice)


def _think(chat_id: int, user_id: int, name: str, text: str,
           by_voice: bool) -> None:
    """Собственно работа. Идёт в потоке чата, приём сообщений не держит."""
    started = time.time()
    progress = Progress(chat_id)

    def remember_proc(proc) -> None:
        with _qlock:
            _running[chat_id] = proc

    try:
        with Typing(chat_id):
            # Помечаем голосовой ввод: иначе помощник не знает, что
            # сообщение пришло звуком, и отвечает про голосовые невпопад.
            asked = (f"[сообщение пришло голосом, расшифровано]\n{text}"
                     if by_voice else text)
            res = brain.answer(asked, history=list(history[chat_id]),
                               chat_key=str(chat_id),
                               on_step=progress.step,
                               on_start=remember_proc,
                               model=chat_model(chat_id))
    except Exception:
        log("ошибка мозга:\n" + traceback.format_exc())
        send(chat_id, "Не смог ответить: внутренняя ошибка. "
                      "Она записана в лог, спросите иначе или позже.")
        return
    finally:
        progress.done()

    took = time.time() - started
    if res.get("reason"):
        log(f"сбой движка: {res['reason']}")
    if res.get("changed"):
        # Помощник переписал себе инструкции — это видно в летописи, но в
        # логе тоже нужно: иначе странное поведение нечем будет объяснить.
        log(f"ИЗМЕНИЛ СВОИ ИНСТРУКЦИИ: {', '.join(res['changed'])}")
    log(f"{name}: «{text[:60]}» → {res['engine']}, "
        f"{', '.join(res['tools']) or 'без инструментов'}, {took:.1f} c, "
        f"токенов {res['tokens']['in']}→{res['tokens']['out']}"
        + (f", ${res['cost']:.3f}" if res.get("cost") else ""))

    out = res["text"]
    if TRACE:
        out += (f"\n\n— {res['engine']}, {took:.1f} c, "
                f"инструменты: {', '.join(res['tools']) or 'нет'}")
    message_id = send(chat_id, out, listen=speech.offered())
    remember(chat_id, message_id, res["text"])
    if speech.wanted(by_voice):
        reply_voice(chat_id, res["text"])
    history[chat_id] += [{"role": "user", "content": text},
                         {"role": "assistant", "content": res["text"]}]


def on_button(query: dict) -> None:
    """Нажали «Прослушать» — озвучиваем тот самый ответ."""
    msg = query.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    user_id = (query.get("from") or {}).get("id")
    key = (chat_id, msg.get("message_id"))

    if user_id not in ALLOW:
        api("answerCallbackQuery", callback_query_id=query["id"],
            text="Доступ закрыт.", show_alert=True)
        return

    text = spoken.get(key)
    if not text:
        api("answerCallbackQuery", callback_query_id=query["id"],
            text="Этот ответ я уже не держу в памяти — спросите заново.",
            show_alert=True)
        api("editMessageReplyMarkup", chat_id=chat_id,
            message_id=msg.get("message_id"))
        return

    api("answerCallbackQuery", callback_query_id=query["id"], text="Озвучиваю…")
    # Кнопку убираем сразу: второе нажатие пока идёт синтез ни к чему.
    api("editMessageReplyMarkup", chat_id=chat_id, message_id=msg.get("message_id"))
    started = time.time()
    reply_voice(chat_id, text, reply_to=msg.get("message_id"))
    log(f"озвучено по кнопке за {time.time() - started:.1f} c")


def publish_commands() -> None:
    """Показывает список команд в меню телеграма — по кнопке «/» в чате.

    Телеграм принимает в меню только латиницу, поэтому туда идут латинские
    имена команд; кириллические двойники продолжают работать как раньше,
    просто в списке не показываются.
    """
    commands = [
        ("help", "что я умею"),
        ("model", "какой моделью отвечать: обычной или умной"),
        ("jobs", "что я сейчас разбираю"),
        ("settings", "как меня настраивали и как вернуть назад"),
        ("stop", "бросить то, что считаю сейчас"),
        ("reset", "забыть разговор и начать с чистого листа"),
        ("id", "показать мой телеграм-id"),
    ]
    # Команды по сообществу показываем, только если база и правда есть:
    # на чужой установке они ведут в пустоту и сбивают с толку.
    try:
        if Path(os.environ.get("VERSHINA_KB_DB", "")).exists():
            commands[3:3] = [("svodka", "общая сводка по потоку"),
                             ("quiet", "кто затих больше недели")]
    except OSError:
        pass

    out = api("setMyCommands", commands=[{"command": c, "description": d}
                                         for c, d in commands])
    if out.get("ok"):
        log(f"список команд в телеграме обновлён: {len(commands)}")
    else:
        log(f"не удалось обновить список команд: {str(out)[:120]}")


def main() -> None:
    if not TOKEN:
        sys.exit("нет VERSHINA_BOT_TOKEN — токен бота берётся у @BotFather")
    me = api("getMe")
    if not me.get("ok"):
        sys.exit("токен не принят телеграмом")
    log(f"бот @{me['result']['username']} запущен, движок {brain.engine_name()}, "
        f"разрешено id: {sorted(ALLOW) or 'никому (список пуст)'}")

    publish_commands()

    # Работник долгих задач: забирает отложенное и присылает результат сам.
    threading.Thread(target=jobs_worker, daemon=True).start()

    conn = store.connect()
    offset = offset_get(conn)
    while True:
        upd = api("getUpdates", offset=offset, timeout=50,
                  allowed_updates=["message", "callback_query"])
        for u in upd.get("result", []):
            offset = u["update_id"] + 1
            offset_set(conn, offset)
            if u.get("callback_query"):
                try:
                    on_button(u["callback_query"])
                except Exception:
                    log("ошибка кнопки:\n" + traceback.format_exc())
                continue

            msg = u.get("message") or {}
            frm = msg.get("from") or {}
            if frm.get("is_bot"):
                continue
            text = (msg.get("text") or msg.get("caption") or "").strip()
            by_voice = bool(msg.get("voice") or msg.get("video_note") or msg.get("audio"))
            name = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x)
            if by_voice and frm.get("id") in ALLOW:
                heard = hear(msg["chat"]["id"], msg)
                if not heard:
                    send(msg["chat"]["id"],
                         "Не разобрал, что записано. Попробуй ещё раз или напиши текстом.")
                    continue
                text = heard
            if not text:
                continue
            try:
                handle(msg["chat"]["id"], frm.get("id", 0), name or "без имени", text,
                       by_voice=by_voice)
            except Exception:
                log("необработанная ошибка:\n" + traceback.format_exc())
        if not upd.get("ok"):
            time.sleep(5)


if __name__ == "__main__":
    main()
