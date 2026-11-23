"""Веб-приложение сообщества «Вершина».

Локальный сервер: запуск через run.sh или
    python3 -m uvicorn app.main:app --reload --port 8000

Данные берутся из базы знаний (только чтение), пользователи и журнал доступа —
из app.sqlite.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import sqlite3
import urllib.error
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import access, analysis, board, finance, call_digest, tracker, call_guess, calls, chatui, claude_env, corpus, decl, fourday, hall, join, emoji, flows, ledger, person_info, posts, reports, reviews, roster, selection, sensation, setup, staff, store, tg_account, ydisk
from . import setup_routes
from . import rows as flowrows
from .config import AVATAR_DIR, PAGE, SESSION_COOKIE, SESSION_DAYS

BASE = Path(__file__).resolve().parent

app = FastAPI(title="Вершина", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))
# Версия статики для ссылок вида /static/style.css?v=…: после выкладки
# браузер берёт свежие стили и скрипты, а не старые из кэша.
templates.env.globals["v"] = str(int(max(
    f.stat().st_mtime for f in (BASE / "static").rglob("*") if f.is_file())))
templates.env.filters["fmt"] = lambda n: (
    "—" if n is None else f"{n:,}".replace(",", "\u00a0") if isinstance(n, int)
    else n)

# Мастер первоначальной настройки. Пока он не пройден, приложению
# показывать нечего: ни пользователей, ни данных ещё нет.
app.include_router(setup_routes.bind(templates))

OPEN_PATHS = ("/setup", "/static", "/favicon.ico")


@app.middleware("http")
async def _setup_gate(request: Request, call_next):
    """До конца настройки любой адрес ведёт в мастер."""
    path = request.url.path
    if not path.startswith(OPEN_PATHS) and not setup.is_done():
        return RedirectResponse("/setup", status_code=307)
    return await call_next(request)


MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря")


def _date_ru(value: str) -> str:
    """2026-09-03 → 03.09.2026. Даты в интерфейсе читают люди, а не машины."""
    try:
        y, m_, d = str(value)[:10].split("-")
        return f"{d}.{m_}.{y}"
    except ValueError:
        return str(value)


def _month_ru(value: str) -> str:
    """2026-04 → апрель 2026."""
    try:
        y, m_ = str(value).split("-")[:2]
        name = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль",
                "август", "сентябрь", "октябрь", "ноябрь", "декабрь")[int(m_) - 1]
        return f"{name} {y}"
    except (ValueError, IndexError):
        return str(value)


def _stamp_ru(value: str) -> str:
    """2026-09-10 10:30:10 → 10.09.2026 13:30.

    sqlite пишет datetime('now') по Гринвичу — показываем по Москве."""
    raw = str(value)
    if len(raw) < 16:
        return _date_ru(raw)
    try:
        from datetime import datetime as _dt, timedelta as _td
        moment = _dt.strptime(raw[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S") + _td(hours=3)
        raw = moment.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass
    return f"{_date_ru(raw)} {raw[11:16]}".strip()


def _money_ru(value) -> str:
    """12500 → «12 500 ₽», минус остаётся минусом."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return "—"
    sign = "−" if n < 0 else ""
    return f"{sign}{abs(n):,}".replace(",", "\u00a0") + "\u00a0₽"


templates.env.filters["money"] = _money_ru
templates.env.filters["d"] = _date_ru
templates.env.filters["month"] = _month_ru
templates.env.filters["stamp"] = _stamp_ru
templates.env.filters["fromjson"] = lambda s: (json.loads(s) if s else [])


@app.on_event("startup")
def _startup() -> None:
    store.init()
    with store.connect() as conn:
        reviews.init(conn)
        roster.init(conn)
        decl.init(conn)
        fourday.init(conn)
        join.init(conn)
        person_info.init(conn)
        posts.init(conn)
        calls.init(conn)
        call_digest.init(conn)
        tracker.init(conn)
        reports.init(conn)
        emoji.init(conn)
        finance.init(conn)
        ydisk.init(conn)
        sensation.init(conn)
        flowrows.init(conn)
        store.drop_expired_sessions(conn)
    # процессов uvicorn несколько — фоновые задачи запускает только один из них
    from .proclock import leader
    if leader("background"):
        roster.start_background()
        reports.start_background()
        hall.start_background()
        fourday.start_background()
        finance.start_background()
        reviews.resume()
        ydisk.start_background()
        sensation.start_background()


# ------------------------------------------------------------------ основа

def db() -> sqlite3.Connection:
    """Подключение на запрос. Работает в режиме автофиксации — см. store.connect."""
    conn = store.connect()
    try:
        yield conn
    finally:
        conn.close()


def current_user(request: Request,
                 conn: sqlite3.Connection = Depends(db)) -> sqlite3.Row:
    user = store.session_user(conn, request.cookies.get(SESSION_COOKIE))
    if not user:
        raise HTTPException(status_code=307, detail="/login",
                            headers={"Location": "/login"})
    return user


@app.exception_handler(RequestValidationError)
def _bad_request(request: Request, exc: RequestValidationError):
    """Сырой JSON валидатора — не ответ для человека."""
    return templates.TemplateResponse(
        request, "error.html",
        {"code": 400, "message": "Странный адрес: в ссылке испорчен один "
                                 "из параметров. Вернитесь и попробуйте снова."},
        status_code=400)


@app.exception_handler(StarletteHTTPException)
def _redirect_unauth(request: Request, exc: StarletteHTTPException):
    # Starlette-овский класс ловит и «нет такого адреса», а не только наши HTTPException
    if exc.status_code == 307 and exc.headers and "Location" in exc.headers:
        return RedirectResponse(exc.headers["Location"])
    message = {"Not Found": "Такой страницы нет — возможно, раздел убрали или ссылка устарела.",
               "Method Not Allowed": "Так эту страницу открыть нельзя."}.get(exc.detail, exc.detail)
    return templates.TemplateResponse(
        request, "error.html",
        {"code": exc.status_code, "message": message}, status_code=exc.status_code)


def page(request: Request, name: str, user: sqlite3.Row,
         ctx: dict[str, Any] | None = None) -> HTMLResponse:
    over = corpus.overview()
    data = {
        "user": user,
        "role_title": store.ROLES.get(user["role"], user["role"]),
        "nav": access.nav(user),
        "hidden_note": access.hidden_note(user["role"]),
        "path": request.url.path,
        # Строка происхождения данных внизу страницы. Участнику она короткая:
        # объёмы корпуса и число расшифровок — не то, что ему полагается видеть.
        "prov": {"messages": over["messages"], "people": over["participants"],
                 "spoken": over["spoken"], "last": over["last"],
                 "full": access.sees_messages(user["role"])},
    }
    data["motion"] = _motion_of(user["id"])
    data.update(ctx or {})
    return templates.TemplateResponse(request, name, data)


def _motion_of(uid: int) -> str:
    """Анимации пользователя: on | off | auto (как в системе)."""
    c = store.connect()
    try:
        return store.get_setting(c, f"ui:motion:{uid}", "auto") or "auto"
    finally:
        c.close()


def as_int(value: str | int | None, default: int | None = None) -> int | None:
    """Число из строки запроса. Форма присылает пустую строку, когда в списке
    выбрано «все» — для маршрута это должно значить «без фильтра», а не отказ."""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def require(flag: bool) -> None:
    if not flag:
        raise HTTPException(status_code=403,
                            detail="Этот раздел недоступен для вашей роли.")


# ------------------------------------------------------------------ вход

@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, conn: sqlite3.Connection = Depends(db)):
    if store.session_user(conn, request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse("/")
    return templates.TemplateResponse(request, "login.html", {"error": request.query_params.get("err"),
                                                             "tg_bot": _tg_bot(conn)})


@app.post("/login")
def login(request: Request, login: str = Form(...), password: str = Form(...),
          conn: sqlite3.Connection = Depends(db)):
    # от подбора пароля: после 8 неудачных попыток за 15 минут логин закрыт на эти 15 минут
    # (по журналу — переживает перезапуск; до 19.10 перебор не ограничивался ничем)
    tries = conn.execute("SELECT COUNT(*) FROM access_log WHERE action='неудачный вход' AND lower(target)=lower(?) "
                         "AND at > datetime('now', '-15 minutes')", (login.strip(),)).fetchone()[0]
    if tries >= 8:
        store.log(conn, None, "вход закрыт: перебор", login)
        return templates.TemplateResponse(
            request, "login.html", {"error": "Слишком много неверных попыток. Подождите 15 минут или войдите через Telegram.",
                                    "tg_bot": _tg_bot(conn)}, status_code=429)
    user = store.user_by_login(conn, login)
    if not user or not store.check_password(password, user["salt"], user["pwhash"]):
        store.log(conn, None, "неудачный вход", login.strip())
        return templates.TemplateResponse(
            request, "login.html", {"error": "Неверный логин или пароль.", "tg_bot": _tg_bot(conn)},
            status_code=401)
    token = store.open_session(conn, user["id"])
    store.log(conn, user, "вход")
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                    max_age=SESSION_DAYS * 86400)
    return resp


def _tg_bot(conn: sqlite3.Connection) -> str:
    return join.bot(conn)["name"] if join.configured(conn) else ""


def _session_redirect(conn: sqlite3.Connection, user_id: int, to: str = "/") -> RedirectResponse:
    token = store.open_session(conn, user_id)
    resp = RedirectResponse(to, status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=SESSION_DAYS * 86400)
    return resp


@app.get("/login/tg")
def login_tg(request: Request, conn: sqlite3.Connection = Depends(db)):
    """«Войти через Telegram» на странице входа."""
    from urllib.parse import quote
    try:
        tg = join.verify(conn, dict(request.query_params))
    except join.JoinError as e:
        return RedirectResponse("/login?err=" + quote(str(e)), status_code=303)
    user = join.login_user(conn, tg)
    if not user:
        waiting = conn.execute("SELECT 1 FROM join_requests WHERE tg_id=? AND status='ждёт'", (tg["tg_id"],)).fetchone()
        store.log(conn, None, "неудачный вход через Telegram", tg.get("username") or str(tg["tg_id"]))
        return RedirectResponse("/login?err=" + quote(
            "Ваша заявка ещё на рассмотрении — ассистент откроет доступ." if waiting else
            "Этот телеграм не связан с платформой. Войдите по ссылке от менеджера."), status_code=303)
    store.log(conn, user, "вход", "через Telegram")
    return _session_redirect(conn, user["id"], "/me" if access.is_participant(user["role"]) else "/")


@app.get("/join/{token}", response_class=HTMLResponse)
def join_page(request: Request, token: str, conn: sqlite3.Connection = Depends(db)):
    """Ссылка-приглашение: «Войти через Telegram» → заявка или сразу карточка."""
    ctx: dict[str, Any] = {"token": token, "tg_bot": _tg_bot(conn), "error": None, "f": None, "person": None}
    try:
        ln = join.active_link(conn, token)
        ctx["f"] = flows.get(conn, ln["flow_id"])
        if ln["person_id"]:
            ctx["person"] = conn.execute("SELECT name FROM people WHERE id=?", (ln["person_id"],)).fetchone()
    except join.JoinError as e:
        ctx["error"] = str(e)
    return templates.TemplateResponse(request, "join.html", ctx)


@app.get("/join/{token}/tg")
def join_tg(request: Request, token: str, conn: sqlite3.Connection = Depends(db)):
    from urllib.parse import quote
    try:
        tg = join.verify(conn, dict(request.query_params))
        res = join.arrive(conn, token, tg)
    except join.JoinError as e:
        return templates.TemplateResponse(request, "join.html", {"token": token, "error": str(e), "f": None})
    if res["kind"] == "вход":
        u = conn.execute("SELECT * FROM users WHERE id=?", (res["user_id"],)).fetchone()
        store.log(conn, u, "вход", "по ссылке-приглашению")
        return _session_redirect(conn, res["user_id"], "/me")
    store.log(conn, None, "заявка на доступ", tg.get("username") or str(tg["tg_id"]))
    return RedirectResponse(f"/join/{token}/sent?r={res['request']}&s={quote(res['secret'])}", status_code=303)


@app.get("/join/{token}/sent", response_class=HTMLResponse)
def join_sent(request: Request, token: str, r: int = 0, s: str = "", saved: int = 0,
              conn: sqlite3.Connection = Depends(db)):
    try:
        req = join.request_by_secret(conn, r, s)
    except join.JoinError as e:
        return templates.TemplateResponse(request, "join.html", {"token": token, "error": str(e), "f": None})
    return templates.TemplateResponse(request, "join.html", {
        "token": token, "step": "sent", "req": req, "saved": saved, "error": None, "tg_bot": _tg_bot(conn),
        "f": flows.get(conn, req["flow_id"])})


@app.post("/join/{token}/sent")
def join_sent_save(request: Request, token: str, r: int = Form(0), s: str = Form(""), full_name: str = Form(""),
                   phone: str = Form(""), photo: UploadFile | None = File(default=None),
                   conn: sqlite3.Connection = Depends(db)):
    from urllib.parse import quote
    try:
        data = photo.file.read(10_000_000) if photo is not None and photo.filename else None
        join.complete(conn, r, s, full_name, phone, data)
    except join.JoinError as e:
        return templates.TemplateResponse(request, "join.html", {"token": token, "error": str(e), "f": None})
    return RedirectResponse(f"/join/{token}/sent?r={r}&s={quote(s)}&saved=1", status_code=303)


@app.post("/flows/{fid}/join")
def flow_join(fid: int, action: str = Form(""), rid: int = Form(0), person: str = Form(""), team: str = Form(""),
              pid: int = Form(0), note: str = Form(""), count: str = Form(""), token: str = Form(""),
              user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(db)):
    """Ссылки-приглашения и заявки на доступ потока."""
    from urllib.parse import quote
    require(access.manages_flows(user["role"]))
    _flow_or_404(conn, fid)
    try:
        if action == "invites":
            notes = [x for x in re.split(r"[\r\n]+", note) if x.strip()]
            if not notes:
                notes = [""] * max(0, min(200, as_int(count) or 0))
            if not notes:
                raise join.JoinError("Напишите, кому ссылки (по строке на человека), или сколько их нужно.")
            made = join.new_invites(conn, fid, notes, user["login"])
            store.log(conn, user, "создал одноразовые ссылки", str(fid), f"{len(made)} шт.")
            msg = f"Готово ссылок: {len(made)} — скопируйте их ниже и отправьте каждому свою."
        elif action == "invite_off":
            join.revoke_invite(conn, token); msg = "Ссылка отозвана."
        elif action == "link_new":
            join.new_flow_link(conn, fid, user["login"])
            msg = "Новая общая ссылка готова — прежняя больше не работает."
        elif action == "link_off":
            join.revoke_flow_link(conn, fid)
            msg = "Общая ссылка выключена."
        elif action == "personal":
            join.person_link(conn, pid, user["login"])
            msg = "Личная ссылка готова — скопируйте её в списке участников."
        elif action == "approve":
            p = join.approve(conn, rid, user["login"], as_int(person), as_int(team))
            msg = "Доступ открыт: телеграм связан с карточкой участника."
            store.log(conn, user, "открыл доступ по заявке", str(rid), f"карточка {roster.card_id(p)}")
        elif action == "reject":
            join.reject(conn, rid, user["login"])
            msg = "Заявка отклонена."
        else:
            raise join.JoinError("Неизвестное действие.")
    except join.JoinError as e:
        return RedirectResponse(f"/flows/{fid}?err={quote(str(e))}#join", status_code=303)
    return RedirectResponse(f"/flows/{fid}?msg={quote(msg)}#join", status_code=303)


@app.post("/flows/{fid}/disk")
async def flow_disk(fid: int, request: Request, user: sqlite3.Row = Depends(current_user),
                    conn: sqlite3.Connection = Depends(db)):
    """Разборы с Яндекс Диска: найти папки по ссылке / отметить, кого загружать."""
    from urllib.parse import quote
    require(access.manages_flows(user["role"]))
    f = _flow_or_404(conn, fid)
    form = await request.form()
    action = form.get("action", "")
    try:
        if action == "scan":
            r = await asyncio.to_thread(_disk_scan, fid, str(form.get("url", "")), user["login"])
            msg = f"Найдено папок с разборами: {r['seen']}, новых: {r['new']}. Проверьте, кто есть кто, и нажмите «Загрузить отмеченные»."
            store.log(conn, user, "нашёл разборы на Диске", f["name"], f"{r['seen']} папок")
        elif action == "pick":
            picks = {}
            for k, v in form.items():
                if k.startswith("p-"):
                    iid = int(k[2:])
                    picks[iid] = (as_int(v) or None, form.get(f"go-{iid}") == "1")
            n = ydisk.choose(conn, fid, picks)
            msg = f"В очереди на загрузку: {n}. Записи скачиваются по одной и сразу идут в разбор." if n else "Отметки сохранены."
            if n:
                store.log(conn, user, "поставил разборы с Диска в очередь", f["name"], f"{n} шт.")
        elif action == "forget":
            ydisk.forget(conn, fid, as_int(form.get("item")) or 0)
            msg = "Убрано из списка."
        else:
            raise ValueError("Неизвестное действие.")
    except (ValueError, urllib.error.URLError) as e:
        return RedirectResponse(f"/flows/{fid}?err={quote(str(e))}#disk", status_code=303)
    return RedirectResponse(f"/flows/{fid}?msg={quote(msg)}#disk", status_code=303)


def _disk_scan(fid: int, url: str, who: str) -> dict:
    # своё соединение: обход Диска — десятки запросов, не держим соединение запроса
    conn = store.connect()
    try:
        return ydisk.scan(conn, flows.get(conn, fid), url, who)
    finally:
        conn.close()


@app.post("/flows/{fid}/fourday")
def flow_fourday(fid: int, action: str = Form(""), sheet: str = Form(""), pid: int = Form(0), value: str = Form(""),
                 pids: list[int] = Form(default=[]), team: str = Form(""), name: str = Form(""),
                 username: str = Form(""), phone: str = Form(""),
                 user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(db)):
    """4-дневный мастермайнд: таблица участников, решение «идёт в ММ8», перевод в основной поток."""
    from urllib.parse import quote
    require(access.manages_flows(user["role"]))
    f = _flow_or_404(conn, fid)
    try:
        if action == "sheet":
            fourday.set_sheet(conn, fid, sheet); msg = "Таблица участников сохранена."
            msg += " " + fourday.sync(conn, fid).capitalize() + "."
        elif action == "sync":
            msg = fourday.sync(conn, fid).capitalize() + "."
        elif action == "next":
            fourday.set_next(conn, pid, value); msg = ""
        elif action == "add":
            new = decl.add_loose(conn, fid, name, username, phone); msg = f"«{name}» добавлен(а)."
            store.log(conn, user, "добавил участника", name, f"поток {f['name']}")
        elif action == "transfer":
            if not pids:
                raise decl.DeclError("Отметьте, кого переводить.")
            done = [fourday.transfer(conn, p, as_int(team) or 0, user["login"]) for p in pids]
            store.log(conn, user, "перевёл в основной поток", f["name"], f"{len(done)} чел.")
            msg = f"Переведено в основной поток: {len(done)}."
        else:
            raise decl.DeclError("Неизвестное действие.")
    except (decl.DeclError, ValueError, RuntimeError) as e:
        return RedirectResponse(f"/flows/{fid}?err={quote(str(e))}#fourday", status_code=303)
    except Exception as e:                                   # таблица недоступна и т. п.
        return RedirectResponse(f"/flows/{fid}?err={quote('Не получилось: ' + str(e)[:200])}#fourday", status_code=303)
    return RedirectResponse(f"/flows/{fid}" + (f"?msg={quote(msg)}" if msg else "") + "#fourday", status_code=303)


@app.post("/telegram/login-bot")
def telegram_login_bot(name: str = Form(""), token: str = Form(""), user: sqlite3.Row = Depends(current_user),
                       conn: sqlite3.Connection = Depends(db)):
    """Бот-ключ для «Войти через Telegram». Только администратор."""
    require(access.is_admin(user["role"]))
    try:
        join.set_bot(conn, name, token)
    except join.JoinError as e:
        return _tg_back({"ok": False, "error": str(e)})
    store.log(conn, user, "настроил вход через Telegram", name)
    return _tg_back({"ok": True}, "Вход через Telegram настроен.")


def _hall_people() -> tuple[list[dict], str | None]:
    d = hall.data()
    people = []
    for p in d.get("people", []):
        w = [x for x in re.split(r"\s+", p["name"]) if x]
        people.append(dict(p, ini="".join(x[0] for x in w[:2]).upper()))
    return people, d.get("at")


@app.get("/hall/{k}", response_class=HTMLResponse)
def hall_page(request: Request, k: str, conn: sqlite3.Connection = Depends(db)):
    """«Зал ММ8» — 106 кресел с фото оплативших участников ступени. Без входа,
    по секретной ссылке: её кидают участникам вместе с новостями.
    Кресла рисует браузер — так зал сам дополняется, пока страница открыта."""
    import json as _json
    if not _hall_host(request) or not secrets.compare_digest(k.encode(), hall.key(conn).encode()):
        raise HTTPException(status_code=404)
    people, at = _hall_people()
    seats = [{"x": s["x"], "y": s["y"], "row": s["row"], "no": s["no"], "order": s["order"]}
             for s in hall.seats([])]
    js = lambda v: _json.dumps(v, ensure_ascii=False).replace("</", "<\\/")
    return templates.TemplateResponse(request, "hall.html", {
        "seats_json": js(seats), "people_json": js(people), "at_json": js(at),
        "filled": min(len(people), hall.SEATS), "total": hall.SEATS, "free": max(0, hall.SEATS - len(people)),
        "at_human": _stamp_ru(at) if at else "", "base": f"/hall/{k}",
    }, headers={"X-Robots-Tag": "noindex, nofollow", "Cache-Control": "no-store"})


@app.get("/hall/{k}/data")
def hall_data(request: Request, k: str):
    """Свежий список для открытой страницы (раз в минуту). Без базы — как фото."""
    if not _hall_host(request) or not secrets.compare_digest(k.encode(), hall.key().encode()):
        raise HTTPException(status_code=404)
    people, at = _hall_people()
    return JSONResponse({"people": people, "at": at, "at_human": _stamp_ru(at) if at else ""},
                        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})


def _hall_host(request: Request) -> bool:
    """Зал отвечает только на своём адресе — на домене платформы его нет."""
    return request.headers.get("host", "").split(":")[0] == hall.HOST


@app.get("/hall/{k}/photo/{name}")
def hall_photo(request: Request, k: str, name: str):
    """Без зависимости от базы: фото грузятся десятками одновременно."""
    from fastapi.responses import FileResponse
    if not _hall_host(request) or not secrets.compare_digest(k.encode(), hall.key().encode()) or not re.fullmatch(r"\d+\.jpg", name):
        raise HTTPException(status_code=404)
    path = hall.DIR / "photos" / name
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=600"})


@app.post("/logout")
def logout(request: Request, conn: sqlite3.Connection = Depends(db)):
    token = request.cookies.get(SESSION_COOKIE)
    user = store.session_user(conn, token)
    if user:
        store.log(conn, user, "выход")
    store.close_session(conn, token)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# ------------------------------------------------------------------ обзор

@app.get("/", response_class=HTMLResponse)
def home(request: Request, user: sqlite3.Row = Depends(current_user),
         conn: sqlite3.Connection = Depends(db)):
    over = corpus.overview()
    ctx: dict[str, Any] = {"over": over, "show_money": access.sees_all_profiles(user["role"])}

    if access.is_participant(user["role"]):
        # Участнику на обзоре — его участие в потоке: вовлечения и команда.
        # Ничего про сообщения: сколько писал и что писал — не его экран.
        tg = user["tg_user_id"]
        ctx["me"] = corpus.profile_of(tg) if tg else None
        ctx["mine"] = ledger.totals_of(conn, tg) if tg else None
        team = ledger.team_of(conn, tg) if tg else None
        ctx["team"] = ledger.team_summary(conn, team["id"]) if team else None
        # участник потока на платформе: вовлечённые его потока и его команда
        p = conn.execute("SELECT flow, team_id FROM people WHERE id=?", (-tg,)).fetchone() if tg and tg < 0 else None
        f = flows.get(conn, name=p["flow"]) if p else None
        if f:
            ctx["inv"], ctx["inv_flow"] = board.board(conn, f), f["name"]
            t = conn.execute("SELECT title FROM teams WHERE id=?", (p["team_id"],)).fetchone() if p["team_id"] else None
            ctx["inv_mine"] = [x["id"] for x in ctx["inv"]["teams"] if t and x["full"] == t["title"]]
    else:
        # Обзор — про идущие потоки и по их декларациям. Закончившиеся потоки
        # сюда не попадают: после финала они — история, а не «кому помочь».
        # Потоков несколько — сверху крючок, им переключают обзор (flowswitch.js).
        ids = access.visible_ids(conn, user)
        choices = _overview_flows(conn, user, ids)
        names = [c["name"] for c in choices]
        want = request.query_params.get("flow", "")
        cur = flows.current(conn)
        default = names.index(cur["name"]) if cur and cur["name"] in names else 0
        idx = names.index(want) if want in names else default
        ctx["flow_choices"], ctx["flow_index"], ctx["flow_default"] = choices, idx, default
        ctx["flow"] = roster.flow_overview(conn, ids, flow=names[idx]) if names else None
        f = flows.get(conn, name=names[idx]) if names else None
        if f:
            ctx["inv"], ctx["inv_flow"], ctx["inv_mine"] = board.board(conn, f), f["name"], board.my_team_ids(conn, user)
        if request.query_params.get("partial") == "1":
            return page(request, "_home_flow.html", user, ctx)
    if access.is_admin(user["role"]):
        ctx["log_stats"] = store.log_stats(conn)
    return page(request, "home.html", user, ctx)


@app.get("/day", response_class=HTMLResponse)
def day_plan(request: Request, flow: str, d: str,
             user: sqlite3.Row = Depends(current_user),
             conn: sqlite3.Connection = Depends(db)):
    """План дня из календаря «Обзора» — фрагмент для панели справа."""
    require(not access.is_participant(user["role"]))
    from datetime import date as _date
    try:
        _date.fromisoformat(d)
    except ValueError:
        raise HTTPException(status_code=400, detail="Неверная дата.")
    if not flows.get(conn, name=flow):
        raise HTTPException(status_code=404, detail="Такого потока нет.")
    plan = roster.day_plan(conn, flow, access.visible_ids(conn, user), d)
    return page(request, "_day_plan.html", user, {"plan": plan})


def _overview_flows(conn: sqlite3.Connection, user: sqlite3.Row,
                    ids: list[int] | None) -> list[dict]:
    """Потоки для крючка «Обзора», слева направо по дате старта.
    Организаторы видят идущие и готовящиеся, ментор — идущие, где у него есть люди."""
    from datetime import date as _date
    organizer = access.manages_flows(user["role"])
    out = []
    for f in flows.listing(conn):
        # готовящийся поток ментор видит, если в нём уже есть его команда
        if f["status"] == "завершён" or (f["status"] == "готовится" and not organizer and ids is access.ALL):
            continue
        if ids is not access.ALL and not roster.roster(conn, f["name"], ids):
            continue
        sub = f["status"]
        if f["status"] == "готовится" and f["start"]:
            sub = f"старт {_date_ru(f['start'])[:5]}"
        elif f["start"] and f["start"] > _date.today().isoformat():
            sub = f"старт {_date_ru(f['start'])[:5]}"
        elif f["start"]:
            day = (_date.today() - _date.fromisoformat(f["start"])).days + 1
            if f["finish"]:
                total = (_date.fromisoformat(f["finish"]) - _date.fromisoformat(f["start"])).days + 1
                sub = f"день {day} из {total}" if day <= total else "финиш прошёл"
            else:
                sub = f"день {day}"
        out.append({"name": f["name"], "sub": sub, "status": f["status"],
                    "start": f["start"] or "9999", "id": f["id"]})
    out.sort(key=lambda c: (c["start"], c["id"]))
    return out


# ------------------------------------------------------------------ участники

@app.get("/participants", response_class=HTMLResponse)
def participants(request: Request, view: str = "", flow: str = "", chat: str = "", status: str = "",
                 team: str = "", q: str = "", user: sqlite3.Row = Depends(current_user),
                 conn: sqlite3.Connection = Depends(db)):
    """Потоки — вкладками (текущий первым), по декларациям; закончившиеся до
    платформы ММ5 и ММ6 — вкладкой «Архив», по переписке и без состояний тишины."""
    require(not access.is_participant(user["role"]))
    ids = access.visible_ids(conn, user)
    needle = q.strip().lstrip("@").lower()
    # вкладки — потоки, где у этой роли кто-то виден
    tabs = []
    for f in flows.listing(conn):
        n = len(roster.roster(conn, f["name"], ids))
        # пустой поток — вкладкой только тем, кто видит всех (организатор ждёт таблиц);
        # ментору — лишь потоки, где есть его люди
        if n or (f["is_current"] and ids is access.ALL):
            tabs.append({"name": f["name"], "count": n, "current": f["is_current"],
                         "status": f["status"]})
    chosen = next((t for t in tabs if t["name"] == flow), None) or (tabs[0] if tabs else None)
    archive = view == "archive" or chosen is None
    flow_all = [] if archive else roster.roster(conn, chosen["name"], ids)
    ctx: dict[str, Any] = {"view": "archive" if archive else "flow", "q": q, "tabs": tabs,
                           "flow_name": chosen["name"] if chosen else None,
                           "flow_status": chosen["status"] if chosen else None,
                           "archive_count": len(corpus.participants(only=ids))}

    if not archive:
        teams = sorted({p["team_title"] for p in flow_all if p["team_title"]})
        people = flow_all
        if needle:
            people = [p for p in people if needle in p["name"].lower()]
        if team:
            people = [p for p in people if p["team_title"] == team]
        for p in people:
            p["state"] = ("выбыл" if p["status"] == "выбыл" else
                          "нет в таблицах" if p["status"] == "нет в таблицах" else
                          "отстаёт" if p["late_steps"] >= 5 else "в графике")
        if status:
            people = [p for p in people if p["state"] == status]
        f = flows.get(conn, name=chosen["name"])
        team_mentors = {t["title"]: ", ".join(m["name"] for m in t["mentors"])
                        for t in flows.teams(conn, f["id"])} if f else {}
        hist = reports.history(conn, f) if f else {"has_chat": False, "stats": {}}
        ctx.update({
            "flow_people": people, "teams": teams, "team": team, "status": status,
            "team_mentors": team_mentors, "rep": hist,
            "flow_sync": roster.last_sync(conn, chosen["name"]),
            "photos": {p["card"] for p in people if has_avatar(conn, p["card"])},
            "show_money": access.sees_all_profiles(user["role"]),
            "native_flow": bool(flow_all) and all(p["source"] == "платформа" for p in flow_all),
        })
        return page(request, "participants.html", user, ctx)

    chat_id = as_int(chat)
    people = corpus.participants(chat_id=chat_id, only=ids)
    if needle:
        # «@anna_member» и «anna_member» — один и тот же запрос.
        people = [p for p in people if needle in p["name"].lower()
                  or needle in (p["username"] or "").lower()]
    ctx.update({
        "people": people, "chats": corpus.overview()["chats"], "chat": chat_id,
        "may_search": access.sees_all_profiles(user["role"]),
        "photos": {p["user_id"] for p in people if has_avatar(conn, p["user_id"])},
        # В выгрузке Telegram Desktop username нет ни у кого: их отдаёт только
        # выгрузка через аккаунт или бота. Пока ищем по имени и честно об этом пишем.
        "no_usernames": not any(p["username"] for p in people),
        "material": {p["user_id"]: analysis.readiness(p["user_id"])["level"]
                     for p in people},
        "portraits": {r["tg_user_id"] for r in conn.execute(
            "SELECT tg_user_id FROM analyses WHERE kind='участнику' AND status='готов'")},
        "mentors": store.assignments_by_participant(conn)
        if access.sees_all_profiles(user["role"]) else {},
    })
    return page(request, "participants.html", user, ctx)


@app.get("/me")
def me(request: Request, user: sqlite3.Row = Depends(current_user)):
    if not user["tg_user_id"]:
        raise HTTPException(
            status_code=404,
            detail="Ваш аккаунт пока не связан с участником в базе сообщества. "
                   "Это делает координатор.")
    return RedirectResponse(f"/participants/{user['tg_user_id']}")


@app.get("/participants/{tg_id}", response_class=HTMLResponse)
def participant(request: Request, tg_id: int, page_no: str = "1", kind: str = "",
                chat: str = "", user: sqlite3.Row = Depends(current_user),
                conn: sqlite3.Connection = Depends(db)):
    if not access.may_open(conn, user, tg_id):
        raise HTTPException(status_code=403,
                            detail="Этот профиль не входит в ваш доступ.")
    if roster.is_roster(tg_id):
        return person_card(request, tg_id, user, conn)
    prof = corpus.profile_of(tg_id)
    if not prof:
        raise HTTPException(status_code=404, detail="Участник не найден.")

    own = user["tg_user_id"] == tg_id
    if not own:
        store.log(conn, user, "открыл профиль", prof["name"], f"tg {tg_id}")

    show_talk = access.sees_messages(user["role"])
    chat_id = as_int(chat)
    page_num = max(1, as_int(page_no, 1) or 1)
    rows, total = ([], 0)
    offset = (page_num - 1) * PAGE
    if show_talk:
        rows, total = corpus.messages_of(tg_id, chat_id=chat_id, offset=offset,
                                         limit=PAGE, spoken_only=(kind == "voice"))

    # Разбор участника моделью: доступен тем, кто ведёт поток, не самому участнику.
    may_analyze = not access.is_participant(user["role"])
    report = analysis.latest(conn, tg_id, "ментору") if may_analyze else None
    portrait = analysis.latest(conn, tg_id, "участнику")
    material = analysis.readiness(tg_id)
    self_profile = conn.execute(
        "SELECT * FROM participant_profile WHERE tg_user_id=?", (tg_id,)).fetchone()

    # Вовлечения и деньги: свои видит и сам участник, чужие — только команда потока.
    show_money = not access.is_participant(user["role"])
    totals = ledger.totals_of(conn, tg_id)
    involvements = ledger.involvements_of(conn, tg_id)
    charges = ledger.charges_of(conn, tg_id)
    team = ledger.team_of(conn, tg_id)
    return page(request, "participant.html", user, {
        "p": prof, "rows": rows, "total": total, "page_no": page_num,
        "pages": max(1, -(-total // PAGE)), "kind": kind, "chat": chat_id,
        "own": own,
        "labels": METRIC_LABELS if show_talk
        else [m for m in METRIC_LABELS if m[0] in OWN_METRICS],
        "show_talk": show_talk,
        "may_analyze": may_analyze, "report": report,
        "portrait": portrait, "material": material,
        "portrait_html": analysis.to_html(portrait["text"])
        if portrait and portrait["status"] == "готов" and portrait["text"] else None,
        "self_profile": self_profile,
        "may_edit_self": own or access.edits_results(user["role"]),
        "totals": totals, "involvements": involvements,
        "charges": charges if show_money else [], "show_money": show_money,
        "team": team, "may_edit": access.edits_results(user["role"]),
        "statuses": ledger.STATUSES, "kinds": ledger.KINDS,
        "today": __import__("datetime").date.today().isoformat(),
        "report_html": analysis.to_html(report["text"])
        if report and report["status"] == "готов" and report["text"] else None,
        "chats": corpus.overview()["chats"],
        "mentor_view": user["role"] == "mentor",
        "mentors": store.assignments_by_participant(conn).get(tg_id, [])
        if access.sees_all_profiles(user["role"]) else [],
        "may_lead": access.leads_people(user["role"]),
        "photo": has_avatar(conn, tg_id),
        "reviews": [dict(r, summary_html=analysis.to_html(reviews.summary_without_goals(r["summary"])))
                    for r in reviews.of(conn, tg_id)] if may_analyze else [],
        "goals": reviews.goals_of(conn, tg_id),
        "goal_statuses": reviews.GOAL_STATUSES,
        **_decl_ctx(conn, tg_id),
    })


def person_card(request: Request, tg_id: int, user: sqlite3.Row, conn: sqlite3.Connection):
    """Карточка участника нового потока: пока по таблице декларации."""
    person = roster.person(conn, tg_id)
    if not person:
        raise HTTPException(status_code=404, detail="Участник не найден.")
    store.log(conn, user, "открыл профиль", person["name"], f"поток {person['flow']}")
    goals = roster.goals_of(conn, person["id"])
    may_analyze = not access.is_participant(user["role"])
    own = access.is_participant(user["role"]) and user["tg_user_id"] == tg_id
    sync = roster.last_sync(conn, person["flow"])
    return page(request, "person.html", user, {
        "person": person, "goals": goals, "photo": has_avatar(conn, tg_id),
        "late_total": sum(g["late"] for g in goals),
        "involved": roster.involved_of(conn, person["id"]),
        "mentors": store.assignments_by_participant(conn).get(tg_id, []),
        "synced": person["synced_at"],
        "sees_sheet": not access.is_participant(user["role"]),
        "show_money": access.sees_all_profiles(user["role"]),
        "p": {"user_id": tg_id, "name": person["name"]}, "own": own,
        "tracker": tracker.own(conn, person) if own else None,
        "may_analyze": may_analyze,
        "may_lead": access.leads_people(user["role"]),
        "reviews": [dict(r, summary_html=analysis.to_html(reviews.summary_without_goals(r["summary"])))
                    for r in reviews.of(conn, tg_id)] if may_analyze else [],
        "goals_mentor": reviews.goals_of(conn, tg_id),
        **_decl_ctx(conn, tg_id),
        "goal_statuses": reviews.GOAL_STATUSES,
        "today": __import__("datetime").date.today().isoformat(),
        "sync_ok": sync["ok"] if sync else None,
        "info": person_info.facts(conn, tg_id, access.sees_all_profiles(user["role"])),
        "may_edit_info": access.leads_people(user["role"]),
        "calls": calls.of_person(conn, tg_id) if access.sees_calls(user["role"]) else None,
        "call_notes": call_digest.of_person(conn, tg_id) if access.sees_calls(user["role"]) else {},
        "rep": _rep_strip(conn, person, tg_id),
        "tab": request.query_params.get("tab", ""),
        "err": request.query_params.get("err"),
        "native": person["source"] == "платформа",
        "may_lead_decl": access.leads_people(user["role"]) and person["source"] == "платформа",
        "may_org_decl": access.edits_results(user["role"]) and person["source"] == "платформа",
        "weeks": decl.weeks(flows.get(conn, name=person["flow"])) if person["source"] == "платформа" else [],
        "money_weeks": decl.money_of(conn, person["id"]) if person["source"] == "платформа" else {},
        "planka": decl.planka_of(conn, person["id"]),
        "sign": decl.sign_state(conn, person["id"]) if person["source"] == "платформа" else None,
        "anketa": fourday.profile(person) if access.sees_all_profiles(user["role"]) else [],
        "fourday_flow": fourday.is_fourday(flows.get(conn, name=person["flow"])),
        "share": decl.share_of(conn, person) if person["source"] == "платформа" else 0,
    })


def _rep_strip(conn: sqlite3.Connection, person: sqlite3.Row, card: int) -> dict | None:
    """Отчёты участника по дням — полоска в карточке: сдал / нет, ссылка на отчёт."""
    f = flows.get(conn, name=person["flow"])
    if not f or person["status"] != "в потоке":
        return None
    h = reports.history(conn, f)
    if not h["has_chat"] or not h["days"]:
        return None
    mine = h["by_card"].get(card, {})
    rest = h["rest"].get(card, set())
    return {"days": [{"d": d, "link": mine.get(d), "ok": d in mine,
                      "off": d not in mine and (reports.is_day_off(d) or d.weekday() in rest)} for d in h["days"]],
            "stat": h["stats"].get(card)}


@app.post("/participants/{tg_id}/profile")
def save_self_profile(tg_id: int, goals: str = Form(""), about: str = Form(""),
                      need: str = Form(""), user: sqlite3.Row = Depends(current_user),
                      conn: sqlite3.Connection = Depends(db)):
    """Цели на мастермайнд и рассказ о себе: пишет сам участник или координатор."""
    own = user["tg_user_id"] == tg_id
    require(own or access.edits_results(user["role"]))
    if not own and not access.may_open(conn, user, tg_id):
        raise HTTPException(status_code=403, detail="Этот профиль не входит в ваш доступ.")
    conn.execute(
        "INSERT INTO participant_profile (tg_user_id, goals, about, need, updated_by) "
        "VALUES (?,?,?,?,?) ON CONFLICT(tg_user_id) DO UPDATE SET "
        "goals=excluded.goals, about=excluded.about, need=excluded.need, "
        "updated_at=datetime('now'), updated_by=excluded.updated_by",
        (tg_id, goals.strip() or None, about.strip() or None, need.strip() or None,
         user["login"]))
    store.log(conn, user, "заполнил цели" if own else "заполнил цели участнику",
              corpus.name_of(tg_id))
    return RedirectResponse(f"/participants/{tg_id}#celi" if not own else "/me#celi",
                            status_code=303)


@app.post("/participants/{tg_id}/portrait")
def participant_portrait(tg_id: int, user: sqlite3.Row = Depends(current_user),
                         conn: sqlite3.Connection = Depends(db)):
    """Собрать портрет, который увидит сам участник."""
    require(access.edits_results(user["role"]))
    if not access.may_open(conn, user, tg_id):
        raise HTTPException(status_code=403, detail="Этот профиль не входит в ваш доступ.")
    if analysis.readiness(tg_id)["level"] == "мало":
        raise HTTPException(
            status_code=400,
            detail="Материала на портрет пока не хватает: по этому участнику "
                   "слишком мало содержательных высказываний. Портрет на таких "
                   "данных будет догадкой.")
    try:
        request_id = analysis.request(conn, tg_id, user, kind="участнику")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    store.log(conn, user, "запросил портрет участнику", corpus.name_of(tg_id),
              f"запрос {request_id}")
    return RedirectResponse(f"/participants/{tg_id}#portret", status_code=303)


@app.post("/participants/{tg_id}/analyze")
def participant_analyze(tg_id: int, user: sqlite3.Row = Depends(current_user),
                        conn: sqlite3.Connection = Depends(db)):
    """Поставить разбор участника в очередь к модели."""
    require(not access.is_participant(user["role"]))
    if not access.may_open(conn, user, tg_id):
        raise HTTPException(status_code=403,
                            detail="Этот профиль не входит в ваш доступ.")
    try:
        request_id = analysis.request(conn, tg_id, user)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    store.log(conn, user, "запросил разбор участника", corpus.name_of(tg_id),
              f"запрос {request_id}, движок {analysis.PROVIDER}")
    return RedirectResponse(f"/participants/{tg_id}#razbor", status_code=303)


METRIC_LABELS = [
    ("messages", "Сообщений", ""),
    ("words", "Слов всего", ""),
    ("avg_words", "Слов в сообщении", ""),
    ("circles", "Кружков", ""),
    ("voice", "Голосовых", ""),
    ("audio_min", "Минут звука", ""),
    ("spoken_share", "Доля голосом", ""),
    ("speech_rate", "Темп речи", "слов/мин"),
    ("pause_share", "Доля пауз в речи", ""),
    ("pauses_per_min", "Остановок в минуту", ""),
    ("longest_pause", "Самая долгая пауза", "с"),
    ("reply_share", "Доля ответов на других", ""),
    ("reply_lag_min", "Задержка ответа", "мин"),
    ("question_share", "Доля сообщений с вопросом", ""),
    ("peak_hour_label", "Пик активности", "МСК"),
    ("night_share", "Доля ночных сообщений", ""),
    ("max_silence_days", "Максимальное молчание", "дней"),
    ("active_months", "Активных месяцев", ""),
]

# Что видит сам участник: как он участвует, без речевых метрик — темп речи
# и паузы считаются по расшифровкам, а значит выдают, что они существуют.
OWN_METRICS = {"messages", "words", "avg_words", "circles", "reply_share",
               "question_share", "peak_hour_label", "max_silence_days",
               "active_months"}


# ------------------------------------------------------------------ поиск

@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = "", chat: str = "",
           spoken: str = "all", user: sqlite3.Row = Depends(current_user),
           conn: sqlite3.Connection = Depends(db)):
    # Поиск идёт по переписке закончившихся потоков — он нужен тем, кто ведёт
    # сообщество целиком; у ментора там никого из его участников нет.
    require(access.sees_all_profiles(user["role"]))
    chat_id = as_int(chat)
    ids = access.visible_ids(conn, user)
    results = corpus.search(conn, q, user_ids=ids, chat_id=chat_id,
                            spoken=spoken) if q else []
    if q:
        store.log(conn, user, "поиск по корпусу", q, f"найдено {len(results)}")
    return page(request, "search.html", user, {
        "q": q, "results": results, "chats": corpus.overview()["chats"],
        "chat": chat_id, "spoken": spoken,
    })


@app.get("/message/{chat_id}/{msg_id}", response_class=HTMLResponse)
def message(request: Request, chat_id: int, msg_id: int,
            user: sqlite3.Row = Depends(current_user),
            conn: sqlite3.Connection = Depends(db)):
    require(access.sees_messages(user["role"]))
    msg = corpus.message(chat_id, msg_id)
    if not msg:
        raise HTTPException(status_code=404, detail="Сообщение не найдено.")
    if not access.may_open(conn, user, msg["user_id"]):
        raise HTTPException(status_code=403,
                            detail="Это сообщение не входит в ваш доступ.")
    allowed = access.visible_ids(conn, user)
    thread = [m for m in corpus.thread_around(chat_id, msg_id)
              if allowed is access.ALL or m["user_id"] in allowed]
    return page(request, "message.html", user, {"msg": msg, "thread": thread})


# ------------------------------------------------------------------ поток

@app.get("/flow", response_class=HTMLResponse)
def flow(request: Request, flow: str = "", user: sqlite3.Row = Depends(current_user),
         conn: sqlite3.Connection = Depends(db)):
    """Текущий поток в динамике — по декларациям. Закончившиеся потоки сюда
    не попадают: их переписка — в «Участниках», вкладка «Архив»."""
    require(access.sees_flow(user["role"]))
    # Цели, по которым человек буксует или выпал: куратору — с кем говорить
    # и кому из менторов подсказать
    mentors = store.assignments_by_participant(conn)
    stuck = [dict(g, name=corpus.name_of(g["tg_user_id"]),
                  mentors=", ".join(mentors.get(g["tg_user_id"], [])))
             for g in conn.execute(
                 "SELECT * FROM goals WHERE status IN ('буксует','выпал') "
                 "ORDER BY status, updated_at DESC").fetchall()]
    return page(request, "flow.html", user, {
        "stuck": stuck,
        "f": roster.flow_overview(conn, flow=flow or None),
        "x": roster.flow_details(conn, flow=flow or None),
        "all_flows": [f for f in flows.listing(conn) if f["status"] != "готовится" or f["name"] == flow],
        "chosen": flow,
    })


# ------------------------------------------------------------------ группы

@app.get("/teams", response_class=HTMLResponse)
def teams(request: Request, user: sqlite3.Row = Depends(current_user),
          conn: sqlite3.Connection = Depends(db)):
    """Команды потока: вовлечённые против своей цели. Ментор видит всю
    таблицу — это соревнование, — а своя команда в ней выделена."""
    require(not access.is_participant(user["role"]))
    # готовящийся поток (например, ММ8 до старта) организаторы видят заранее
    org = access.manages_flows(user["role"])
    choices = [f for f in flows.listing(conn) if f["status"] != "готовится" or org]
    view = request.query_params.get("view", "")
    # архив — все участники ММ5/ММ6 разом: только тем, кто видит всех
    may_archive = access.sees_all_profiles(user["role"])
    if view == "archive" and may_archive:
        return page(request, "teams_archive.html", user, {
            "choices": choices, "rows": ledger.announcements(conn)})
    want = request.query_params.get("flow", "")
    cur = flows.current(conn)
    f = next((x for x in choices if x["name"] == want), None) \
        or next((x for x in choices if cur and x["id"] == cur["id"]), None) or (choices[0] if choices else None)
    mine = board.my_team_ids(conn, user)
    return page(request, "teams.html", user, {
        "choices": choices, "f": f, "b": board.board(conn, f) if f else None, "mine": mine,
        "organizer": access.manages_flows(user["role"]),
        "archive_count": conn.execute("SELECT COUNT(*) FROM involvements").fetchone()[0] if may_archive else 0,
    })


def _may_see_team(user: sqlite3.Row, t: dict) -> bool:
    return access.sees_all_profiles(user["role"]) or user["id"] in t["staff_ids"]


@app.get("/teams/mine")
def my_teams(user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(db)):
    """«Моя команда» ментора и трекера: его команда в текущем потоке (а нет — в любом идущем)."""
    require(not access.is_participant(user["role"]))
    ids = board.my_team_ids(conn, user)
    if not ids:
        return RedirectResponse("/teams", status_code=303)
    cur = flows.current(conn)
    rows = conn.execute(f"SELECT t.id, t.flow_id, f.status FROM flow_teams t JOIN flows f ON f.id=t.flow_id "
                        f"WHERE t.id IN ({','.join('?' * len(ids))})", tuple(ids)).fetchall()
    rows = sorted(rows, key=lambda r: (not (cur and r["flow_id"] == cur["id"]), r["status"] != "идёт", r["id"]))
    return RedirectResponse(f"/teams/{rows[0]['id']}", status_code=303)


@app.post("/me/steps/{sid}")
def my_step(sid: int, user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(db)):
    """Участник отмечает свой шаг сам; ментор и трекер потом подтверждают."""
    from urllib.parse import quote
    require(access.is_participant(user["role"]))
    card = user["tg_user_id"]
    person = roster.person(conn, card) if card else None
    if not person or person["source"] != "платформа":
        raise HTTPException(status_code=403, detail="Шаги в этом потоке отмечают в таблице команды.")
    try:
        done = tracker.self_mark(conn, person["id"], sid, user["name"])
    except decl.DeclError as e:
        return RedirectResponse(f"/participants/{card}?err=" + quote(str(e)) + "#trekker", status_code=303)
    store.log(conn, user, "отметил свой шаг" if done else "снял отметку шага", person["name"], str(sid))
    return RedirectResponse(f"/participants/{card}#trekker", status_code=303)


@app.get("/teams/{team_id}", response_class=HTMLResponse)
def team_page(request: Request, team_id: int,
              user: sqlite3.Row = Depends(current_user),
              conn: sqlite3.Connection = Depends(db)):
    """Команда потока: участники и их вовлечённые. Ментор — только своей."""
    require(not access.is_participant(user["role"]))
    t = board.team(conn, team_id)
    if not t:
        raise HTTPException(status_code=404, detail="Команда не найдена.")
    if not _may_see_team(user, t):
        raise HTTPException(status_code=403, detail="Состав этой команды видят её менторы и организаторы.")
    return page(request, "team_flow.html", user, {
        "t": t, "may_goal": access.manages_flows(user["role"]) or user["id"] in t["staff_ids"],
        "tr": tracker.team(conn, t["flow"], t["full"], board._team_people(conn, t["flow"]["name"], t["full"])),
        "msg": request.query_params.get("msg"), "err": request.query_params.get("err"),
    })


@app.post("/teams/{team_id}/goal")
def team_goal(team_id: int, goal: str = Form(""), user: sqlite3.Row = Depends(current_user),
              conn: sqlite3.Connection = Depends(db)):
    """Цель команды — ставит организатор или ментор/трекер этой команды."""
    from urllib.parse import quote
    t = board.team(conn, team_id)
    if not t:
        raise HTTPException(status_code=404, detail="Команда не найдена.")
    require(access.manages_flows(user["role"]) or user["id"] in t["staff_ids"])
    value = as_int(goal.strip()) if goal.strip() else None
    try:
        flows.set_goal(conn, team_id, value)
    except flows.FlowError as e:
        return RedirectResponse(f"/teams/{team_id}?err=" + quote(str(e)), status_code=303)
    store.log(conn, user, "поставил цель команде", t["full"], str(value or "—"))
    return RedirectResponse(f"/teams/{team_id}?msg=" + quote(
        f"Цель команды: {value} вовлечённых." if value else "Цель снята."), status_code=303)


@app.get("/my-team", response_class=HTMLResponse)
def my_team(request: Request, user: sqlite3.Row = Depends(current_user),
            conn: sqlite3.Connection = Depends(db)):
    require(access.is_participant(user["role"]))
    if not user["tg_user_id"]:
        raise HTTPException(status_code=404,
                            detail="Ваш аккаунт пока не связан с участником сообщества.")
    team = ledger.team_of(conn, user["tg_user_id"])
    if not team:
        raise HTTPException(status_code=404,
                            detail="Вы пока не состоите ни в одной группе потока. "
                                   "Группы собирает координатор.")
    return _team_view(request, user, conn, team["id"], own=True)


def _team_view(request: Request, user: sqlite3.Row, conn: sqlite3.Connection,
               team_id: int, own: bool) -> HTMLResponse:
    summary = ledger.team_summary(conn, team_id)
    if not summary:
        raise HTTPException(status_code=404, detail="Группа не найдена.")
    roles = {m["tg_user_id"]: m["role"] for m in ledger.members(conn, team_id)}
    people = [{"id": t, "name": corpus.name_of(t), "role": roles.get(t, "участник"),
               **summary["per"].get(t, {})} for t in summary["ids"]]
    people.sort(key=lambda p: (-p.get("count", 0), p["name"]))
    return page(request, "team.html", user, {
        "s": summary, "people": people, "own": own,
        "me_tg": user["tg_user_id"],
        # Сколько кто принёс денег — дело команды потока, не участников.
        "show_money": not access.is_participant(user["role"]),
        "may_edit": access.edits_results(user["role"]),
        "candidates": [] if own else corpus.participants(),
    })


# ------------------------------------------------------------------ вовлечения и деньги

@app.post("/participants/{tg_id}/involvement")
def add_involvement(tg_id: int, happened: str = Form(""), amount: str = Form("0"),
                    who: str = Form(""), status: str = Form("оплачено"),
                    note: str = Form(""), user: sqlite3.Row = Depends(current_user),
                    conn: sqlite3.Connection = Depends(db)):
    require(access.edits_results(user["role"]))
    if not access.may_open(conn, user, tg_id):
        raise HTTPException(status_code=403, detail="Этот профиль не входит в ваш доступ.")
    amount_value = int(re.sub(r"[^\d-]", "", amount) or 0)
    ledger.add_involvement(conn, tg_id, happened, amount_value, who, status, note,
                           user["login"])
    store.log(conn, user, "записал вовлечение", corpus.name_of(tg_id),
              f"{amount_value} ₽, {status}")
    return RedirectResponse(f"/participants/{tg_id}#dengi", status_code=303)


@app.post("/participants/{tg_id}/charge")
def add_charge(tg_id: int, happened: str = Form(""), amount: str = Form("0"),
               kind: str = Form("штраф"), reason: str = Form(""),
               user: sqlite3.Row = Depends(current_user),
               conn: sqlite3.Connection = Depends(db)):
    require(access.edits_results(user["role"]))
    if not access.may_open(conn, user, tg_id):
        raise HTTPException(status_code=403, detail="Этот профиль не входит в ваш доступ.")
    amount_value = int(re.sub(r"[^\d-]", "", amount) or 0)
    ledger.add_charge(conn, tg_id, happened, amount_value, kind, reason, user["login"])
    store.log(conn, user, f"записал {kind}", corpus.name_of(tg_id), f"{amount_value} ₽")
    return RedirectResponse(f"/participants/{tg_id}#dengi", status_code=303)


@app.post("/participants/{tg_id}/involvement/{row_id}/remove")
def remove_involvement(tg_id: int, row_id: int, user: sqlite3.Row = Depends(current_user),
                       conn: sqlite3.Connection = Depends(db)):
    require(access.edits_results(user["role"]))
    ledger.drop_involvement(conn, row_id)
    store.log(conn, user, "удалил вовлечение", corpus.name_of(tg_id), f"запись {row_id}")
    return RedirectResponse(f"/participants/{tg_id}#dengi", status_code=303)


@app.post("/participants/{tg_id}/charge/{row_id}/remove")
def remove_charge(tg_id: int, row_id: int, user: sqlite3.Row = Depends(current_user),
                  conn: sqlite3.Connection = Depends(db)):
    require(access.edits_results(user["role"]))
    ledger.drop_charge(conn, row_id)
    store.log(conn, user, "удалил запись о деньгах", corpus.name_of(tg_id), f"запись {row_id}")
    return RedirectResponse(f"/participants/{tg_id}#dengi", status_code=303)


# ------------------------------------------------------------------ фото участников

def avatar_tg(conn: sqlite3.Connection, tg_id: int) -> int | None:
    """Чьё фото показывать: у участника нового потока — когда свяжем его с чатами."""
    if roster.is_roster(tg_id):
        row = conn.execute("SELECT tg_user_id FROM people WHERE id=?", (-tg_id,)).fetchone()
        return row["tg_user_id"] if row and row["tg_user_id"] else None
    return tg_id


def _own_photo(tg_id: int):
    """Фото участника на платформе (из таблицы, загруженное, из «Войти через Telegram»)."""
    if not roster.is_roster(tg_id):
        return None
    p = fourday.photo_path(-tg_id)
    return p if p.exists() else None


def has_avatar(conn: sqlite3.Connection, tg_id: int) -> bool:
    if _own_photo(tg_id):
        return True
    real = avatar_tg(conn, tg_id)
    return bool(real) and (AVATAR_DIR / f"{real}.jpg").exists()


@app.get("/avatar/{tg_id}")
def avatar(tg_id: int, user: sqlite3.Row = Depends(current_user),
           conn: sqlite3.Connection = Depends(db)):
    if not access.may_open(conn, user, tg_id):
        raise HTTPException(status_code=403)
    own = _own_photo(tg_id)
    if own:
        return FileResponse(own, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=600"})
    real = avatar_tg(conn, tg_id)
    path = AVATAR_DIR / f"{real}.jpg" if real else None
    if not path or not path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})


# ------------------------------------------------------------------ разборы по записям и цели

def _decl_ctx(conn: sqlite3.Connection, tg_id: int) -> dict:
    """Шаги к целям и черновики деклараций из разборов — для блоков карточки."""
    gids = [g["id"] for g in reviews.goals_of(conn, tg_id)]
    decl = {r["review_id"]: r for r in conn.execute(
        "SELECT * FROM declarations WHERE tg_user_id=?", (tg_id,))}
    return {"steps": reviews.steps_of(conn, gids), "decls": decl}


@app.post("/participants/{tg_id}/reviews/{rid}/confirm")
def confirm_declaration(tg_id: int, rid: int, user: sqlite3.Row = Depends(current_user),
                        conn: sqlite3.Connection = Depends(db)):
    _lead(conn, user, tg_id)
    reviews.confirm_declaration(conn, rid, user["login"])
    store.log(conn, user, "подтвердил декларацию", corpus.name_of(tg_id), f"разбор {rid}")
    return RedirectResponse(f"/participants/{tg_id}#celi-potok", status_code=303)


@app.post("/participants/{tg_id}/steps/{sid}")
def toggle_step(tg_id: int, sid: int, user: sqlite3.Row = Depends(current_user),
                conn: sqlite3.Connection = Depends(db)):
    _lead(conn, user, tg_id)
    reviews.toggle_step(conn, sid, user["login"])
    return RedirectResponse(f"/participants/{tg_id}#celi-potok", status_code=303)


@app.post("/decl/{pid}")
def decl_edit(pid: int, op: str = Form(...), gid: int = Form(0), sid: int = Form(0), idx: int = Form(-1),
              title: str = Form(""), deadline: str = Form(""), proof: str = Form(""), n: str = Form(""),
              program: str = Form(""), amount: str = Form(""), word_price: str = Form(""), perk: str = Form(""),
              money_goal: str = Form(""), on: str = Form(""), tab: str = Form("decl"),
              user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(db)):
    """Правка декларации участника потока на платформе (без Google-таблицы).
    Цели и шаги — ментор и трекер своей команды; вовлечения, выбывшие и деньги — организаторы."""
    card = roster.card_id(pid)
    _lead(conn, user, card)
    back = f"/participants/{card}?tab={tab}"
    org = access.edits_results(user["role"])
    try:
        p = decl.editable(conn, pid)
        if gid and not conn.execute("SELECT 1 FROM decl_goals WHERE id=? AND person_id=?", (gid, pid)).fetchone():
            raise decl.DeclError("Цель не из этой декларации.")
        if sid and not conn.execute("SELECT 1 FROM decl_steps WHERE id=? AND person_id=?", (sid, pid)).fetchone():
            raise decl.DeclError("Шаг не из этой декларации.")
        if op in ("planka", "inv_add", "inv_remove", "left", "money", "terms") and not org:
            raise decl.DeclError("Вовлечения, деньги и выбывших отмечают организаторы.")
        if op == "goal_add":
            gid = decl.add_goal(conn, pid, title, deadline, proof)
        elif op == "goal_save":
            decl.update_goal(conn, gid, title, deadline, proof)
        elif op == "goal_close":
            decl.close_goal(conn, gid, on == "1")
        elif op == "goal_remove":
            decl.remove_goal(conn, gid)
            gid = 0
        elif op == "step_add":
            decl.add_step(conn, gid, title, deadline)
        elif op == "step_toggle":
            decl.toggle_step(conn, sid, user["login"])
        elif op == "step_confirm":
            tracker.confirm(conn, pid, sid, user["name"])
        elif op == "step_save":
            decl.update_step(conn, sid, title, deadline)
        elif op == "step_remove":
            decl.remove_step(conn, sid)
        elif op == "planka":
            decl.set_planka(conn, pid, as_int(n) or 0)
        elif op == "inv_add":
            decl.add_involvement(conn, pid, deadline, program or "большая программа")
        elif op == "inv_remove":
            decl.remove_involvement(conn, pid, idx)
        elif op == "money":
            decl.set_money(conn, pid, as_int(n) or 0, amount, user["login"])
        elif op == "terms":
            decl.set_terms(conn, pid, word_price, perk, money_goal)
        elif op == "left":
            decl.set_left(conn, pid, on == "1")
        elif op == "version_new":
            no = decl.new_version(conn, pid, user["login"])
            store.log(conn, user, "декларация на подпись", p["name"], f"версия {no}")
            return RedirectResponse(f"/decl/{pid}/print?v={no}", status_code=303)
        elif op == "version_signed":
            decl.mark_signed(conn, pid, as_int(n) or 0, user["login"], deadline)
        elif op == "version_unsign":
            decl.unmark_signed(conn, pid, as_int(n) or 0)
        else:
            raise decl.DeclError("Неизвестное действие.")
    except decl.DeclError as e:
        from urllib.parse import quote
        return RedirectResponse(f"{back}&err={quote(str(e))}", status_code=303)
    store.log(conn, user, "правка декларации", p["name"], op)
    return RedirectResponse(back + (f"#goal-{gid}" if gid else ""), status_code=303)


@app.get("/decl/{pid}/print", response_class=HTMLResponse)
def decl_print(request: Request, pid: int, v: int = 0, user: sqlite3.Row = Depends(current_user),
               conn: sqlite3.Connection = Depends(db)):
    """Декларация на подпись — страница для печати (A4, из браузера или в PDF)."""
    card = roster.card_id(pid)
    _lead(conn, user, card)
    p = decl.person(conn, pid)
    if not p:
        raise HTTPException(status_code=404)
    vs = decl.versions(conn, pid)
    ver = next((x for x in vs if x["no"] == v), None) if v else (vs[0] if vs else None)
    snap = ver["content"] if ver else decl.snapshot(conn, pid)
    signed = next((x for x in vs if x["status"] == "подписана" and (not ver or x["no"] < ver["no"])), None)
    f = flows.get(conn, name=p["flow"])
    t = conn.execute("SELECT title FROM teams WHERE id=?", (p["team_id"],)).fetchone()
    return templates.TemplateResponse(request, "decl_print.html", {
        "p": p, "snap": snap, "ver": ver, "f": f, "team": t["title"] if t else "",
        "mentors": store.assignments_by_participant(conn).get(card, []),
        "changes": decl.diff(signed["content"], snap) if signed else [], "prev": signed,
    })


@app.post("/people/{pid}/photo")
def person_photo(pid: int, file: UploadFile = File(...), user: sqlite3.Row = Depends(current_user),
                 conn: sqlite3.Connection = Depends(db)):
    """Фото участника — для карточки и «Сенсационных новостей». Только организаторы."""
    from urllib.parse import quote
    require(access.edits_results(user["role"]))
    card = roster.card_id(pid)
    if not decl.person(conn, pid):
        raise HTTPException(status_code=404)
    if not fourday.save_photo(conn, pid, file.file.read(10_000_000)):
        return RedirectResponse(f"/participants/{card}?tab=info&err={quote('Не получилось прочитать картинку.')}", status_code=303)
    store.log(conn, user, "загрузил фото участника", decl.person(conn, pid)["name"])
    return RedirectResponse(f"/participants/{card}?tab=info", status_code=303)


@app.post("/decl/team/{ftid}/person")
def decl_add_person(ftid: int, name: str = Form(""), username: str = Form(""), planka: str = Form(""),
                    user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(db)):
    """Участник в команду, которая ведётся на платформе."""
    require(access.manages_flows(user["role"]))
    from urllib.parse import quote
    t = conn.execute("SELECT * FROM flow_teams WHERE id=?", (ftid,)).fetchone()
    if not t:
        raise HTTPException(status_code=404)
    try:
        pid = decl.add_person(conn, ftid, name, username, as_int(planka) or 0)
    except decl.DeclError as e:
        return RedirectResponse(f"/flows/{t['flow_id']}?err={quote(str(e))}#team-{ftid}", status_code=303)
    store.log(conn, user, "добавил участника", name, f"команда {t['title']}")
    return RedirectResponse(f"/participants/{roster.card_id(pid)}", status_code=303)


def _lead(conn: sqlite3.Connection, user: sqlite3.Row, tg_id: int) -> None:
    require(access.leads_people(user["role"]))
    if not access.may_open(conn, user, tg_id):
        raise HTTPException(status_code=403, detail="Этот профиль не входит в ваш доступ.")


@app.post("/participants/{tg_id}/info")
def save_info(tg_id: int, city: str | None = Form(None), work: str | None = Form(None),
              income: str | None = Form(None), request_: str | None = Form(None, alias="request"),
              about: str | None = Form(None), telegram: str | None = Form(None),
              phone: str | None = Form(None),
              user: sqlite3.Row = Depends(current_user),
              conn: sqlite3.Connection = Depends(db)):
    """Сведения о человеке: ментор правит свои, координатор и админ — всех.
    Доход — как и остальные деньги — ментору не виден и не правится.
    Маршрут синхронный: подключение к sqlite живёт в потоке запроса."""
    _lead(conn, user, tg_id)
    form = {k: v for k, v in dict(city=city, work=work, income=income, request=request_,
                                  about=about, telegram=telegram, phone=phone).items()
            if v is not None}
    money = access.sees_all_profiles(user["role"])
    changed = []
    for key, title, _ in person_info.FIELDS:
        if key in person_info.MONEY_FIELDS and not money:
            continue
        if key in form:
            before = conn.total_changes
            person_info.set_fact(conn, tg_id, key, str(form[key]), user["name"])
            if conn.total_changes != before:
                changed.append(title)
    if changed:
        store.log(conn, user, "обновил сведения", corpus.name_of(tg_id), ", ".join(changed))
    return RedirectResponse(f"/participants/{tg_id}?tab=info", status_code=303)


@app.post("/participants/{tg_id}/info/{fid}")
def decide_info(tg_id: int, fid: int, accept: str = Form("0"),
                user: sqlite3.Row = Depends(current_user),
                conn: sqlite3.Connection = Depends(db)):
    _lead(conn, user, tg_id)
    person_info.decide(conn, tg_id, fid, accept == "1", user["name"])
    store.log(conn, user, "принял сведение" if accept == "1" else "отклонил сведение",
              corpus.name_of(tg_id), f"запись {fid}")
    return RedirectResponse(f"/participants/{tg_id}?tab=info", status_code=303)


@app.post("/participants/{tg_id}/reviews")
def add_review(tg_id: int, held_on: str = Form(""), title: str = Form(""),
                     file: UploadFile = File(...), user: sqlite3.Row = Depends(current_user),
                     conn: sqlite3.Connection = Depends(db)):
    _lead(conn, user, tg_id)
    # синхронно: соединение с базой живёт в потоке обработчика, async его не видит
    data = file.file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Файл пустой.")
    try:
        reviews.create(conn, tg_id, title, held_on, file.filename or "", data, user["login"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    store.log(conn, user, "загрузил разбор", corpus.name_of(tg_id), file.filename or "")
    return RedirectResponse(f"/participants/{tg_id}#razbory", status_code=303)


@app.post("/participants/{tg_id}/reviews/{rid}/retry")
def retry_review(tg_id: int, rid: int, user: sqlite3.Row = Depends(current_user),
                 conn: sqlite3.Connection = Depends(db)):
    _lead(conn, user, tg_id)
    reviews.retry(conn, rid)
    return RedirectResponse(f"/participants/{tg_id}#razbory", status_code=303)


@app.post("/participants/{tg_id}/reviews/{rid}/remove")
def remove_review(tg_id: int, rid: int, user: sqlite3.Row = Depends(current_user),
                  conn: sqlite3.Connection = Depends(db)):
    _lead(conn, user, tg_id)
    reviews.remove(conn, rid)
    store.log(conn, user, "удалил разбор", corpus.name_of(tg_id), f"разбор {rid}")
    return RedirectResponse(f"/participants/{tg_id}#razbory", status_code=303)


@app.post("/participants/{tg_id}/goals")
def add_goal(tg_id: int, text: str = Form(""), due: str = Form(""),
             user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(db)):
    _lead(conn, user, tg_id)
    if text.strip():
        reviews.add_goal(conn, tg_id, text, due, user["login"])
        store.log(conn, user, "добавил цель", corpus.name_of(tg_id), text[:80])
    return RedirectResponse(f"/participants/{tg_id}#celi-potok", status_code=303)


@app.post("/participants/{tg_id}/goals/{gid}")
def update_goal(tg_id: int, gid: int, status: str = Form(""), note: str = Form(""),
                user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(db)):
    _lead(conn, user, tg_id)
    reviews.set_goal(conn, gid, status, note, user["login"])
    return RedirectResponse(f"/participants/{tg_id}#celi-potok", status_code=303)


@app.post("/participants/{tg_id}/goals/{gid}/remove")
def remove_goal(tg_id: int, gid: int, user: sqlite3.Row = Depends(current_user),
                conn: sqlite3.Connection = Depends(db)):
    _lead(conn, user, tg_id)
    reviews.remove_goal(conn, gid)
    return RedirectResponse(f"/participants/{tg_id}#celi-potok", status_code=303)


# ------------------------------------------------------------------ созвоны

@app.get("/calls", response_class=HTMLResponse)
def calls_list(request: Request, user: sqlite3.Row = Depends(current_user),
               conn: sqlite3.Connection = Depends(db)):
    """Встречи «Вершины» из Стенографа. Ментор видит те, где были его участники."""
    require(access.sees_calls(user["role"]))
    items = calls.listing(conn, user, access.visible_ids(conn, user))
    # вкладки: потоки, где есть встречи (и текущий), «Вне потоков», «Все»
    order = [f["name"] for f in flows.listing(conn) if f["status"] != "готовится"]
    counts: dict[str, int] = {}
    for c in items:
        counts[c["flow"] or "-"] = counts.get(c["flow"] or "-", 0) + 1
    cur = flows.current(conn)
    tabs = [{"key": n, "title": n, "n": counts.get(n, 0), "current": bool(cur and cur["name"] == n)}
            for n in order if counts.get(n) or (cur and cur["name"] == n)]
    if counts.get("-"):
        tabs.append({"key": "-", "title": "Вне потоков", "n": counts["-"], "current": False})
    want = request.query_params.get("flow")
    if want is None:
        want = next((t["key"] for t in tabs if t["current"] and t["n"]), tabs[0]["key"] if tabs else "*")
    q = request.query_params.get("q", "").strip().lower()
    shown = [c for c in items if (want == "*" or (c["flow"] or "-") == want)
             and (not q or q in calls.title_of(c).lower()
                  or any(q in (p["name"] or "").lower() or q in p["label"].lower() for p in c["people"]))]
    return page(request, "calls.html", user, {
        "items": shown, "total": len(items), "tabs": tabs, "want": want,
        "q": request.query_params.get("q", ""), "title_of": calls.title_of})


@app.get("/calls/{call_id}", response_class=HTMLResponse)
def call_one(request: Request, call_id: str, user: sqlite3.Row = Depends(current_user),
             conn: sqlite3.Connection = Depends(db)):
    require(access.sees_calls(user["role"]))
    c = calls.get(conn, call_id)
    if not c:
        raise HTTPException(status_code=404, detail="Встреча не найдена.")
    ids = access.visible_ids(conn, user)
    if not calls.visible(c, user, ids):
        raise HTTPException(status_code=403, detail="На этой встрече не было ваших участников.")
    store.log(conn, user, "открыл созвон", calls.title_of(c))
    organizer = access.sees_all_profiles(user["role"])
    who_list = []
    if organizer:
        who_list = [{"kind": "участник", "ref": p["card"], "name": p["name"], "flow": p["flow"]}
                    for p in roster.roster(conn) if p["status"] in ("в потоке", "выбыл")] + \
                   [{"kind": "команда", "ref": u["id"], "name": u["name"], "flow": None}
                    for u in conn.execute("SELECT id, name FROM users WHERE active=1 AND role IN "
                                          "('mentor','coordinator','admin') ORDER BY name")]
    run = call_guess.run_state(conn, call_id) if organizer else None
    # новая встреча с неузнанными — разбор запускается сам, когда её открыл организатор
    if organizer and run is None and call_guess.targets(c) and claude_env.token():
        call_guess.start(conn, call_id)
        run = call_guess.run_state(conn, call_id)
    return page(request, "call.html", user, {
        "c": c, "title": calls.title_of(c), "organizer": organizer, "who_list": who_list,
        "guesses": call_guess.of_call(conn, call_id) if organizer else {},
        "run": run, "can_guess": bool(call_guess.targets(c)), "claude_ok": bool(claude_env.token()),
        "names_by_ref": {(w["kind"], w["ref"]): w["name"] for w in who_list},
        "flow_names": [f["name"] for f in flows.listing(conn)],
        "visible_cards": None if ids is None else set(ids),
        "summary_html": analysis.to_html(c["summary"]) if c["summary"] else None,
        "digest": call_digest.of_call(conn, call_id), "digest_run": call_digest.run_state(conn, call_id),
        "can_digest": organizer and call_digest.can_run(c),
        "msg": request.query_params.get("msg"),
    })


@app.get("/calls/{call_id}/word")
def call_word(call_id: str, user: sqlite3.Row = Depends(current_user),
              conn: sqlite3.Connection = Depends(db)):
    """Word со встречей — расшифровка по ролям и выжимка, как его собрал Стенограф."""
    from fastapi.responses import FileResponse
    from urllib.parse import quote
    require(access.sees_calls(user["role"]))
    c = calls.get(conn, call_id)
    path = calls.docx_path(call_id)
    if not c or not path:
        raise HTTPException(status_code=404, detail="Word для этой встречи нет.")
    if not calls.visible(c, user, access.visible_ids(conn, user)):
        raise HTTPException(status_code=403, detail="На этой встрече не было ваших участников.")
    store.log(conn, user, "скачал Word созвона", calls.title_of(c))
    name = f"{calls.title_of(c)} {c['when']:%Y-%m-%d}.docx" if c["when"] else f"{calls.title_of(c)}.docx"
    name = re.sub(r'[\\/:*?"<>|]+', " ", name).strip()
    return FileResponse(path, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


@app.post("/calls/{call_id}/flow")
def call_flow(call_id: str, flow: str = Form(""), user: sqlite3.Row = Depends(current_user),
              conn: sqlite3.Connection = Depends(db)):
    """Указать поток встречи вручную: «-» — вне потоков, пусто — определять самой."""
    from urllib.parse import quote
    require(access.sees_all_profiles(user["role"]))
    c = calls.get(conn, call_id)
    if not c:
        raise HTTPException(status_code=404, detail="Встреча не найдена.")
    if flow not in ("", "-") and not flows.get(conn, name=flow):
        raise HTTPException(status_code=400, detail="Нет такого потока.")
    calls.set_flow(conn, call_id, flow, user["login"])
    store.log(conn, user, "указал поток созвона", calls.title_of(c), flow or "авто")
    return RedirectResponse(f"/calls/{call_id}?msg=" + quote("Поток встречи сохранён."), status_code=303)


@app.post("/calls/{call_id}/who")
def call_who(call_id: str, label: str = Form(...), who: str = Form(""), scope: str = Form("везде"),
             user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(db)):
    """Отметить, кто скрывается за подписью в Зуме. Уникальная подпись — во всех
    встречах, общая («iPhone», «Голос 3») — только в этой."""
    from urllib.parse import quote
    require(access.sees_all_profiles(user["role"]))
    kind, _, ref = who.partition(":")
    here = scope == "встреча"
    calls.set_alias(conn, label, kind, as_int(ref), user["login"], call_id if here else "")
    store.log(conn, user, "отметил говорящего", label, (who or "сброс") + (" (эта встреча)" if here else ""))
    note = "в этой встрече" if here else "во всех встречах"
    return RedirectResponse(f"/calls/{call_id}?msg=" + quote(f"«{label}» отмечен {note}.") + "#who",
                            status_code=303)


@app.post("/calls/{call_id}/guess")
def call_guess_start(call_id: str, user: sqlite3.Row = Depends(current_user),
                     conn: sqlite3.Connection = Depends(db)):
    """Разобрать по разговору, кто такие неузнанные говорящие (Claude, в фоне)."""
    from urllib.parse import quote
    require(access.sees_all_profiles(user["role"]))
    res = call_guess.start(conn, call_id)
    store.log(conn, user, "запустил разбор говорящих", call_id, res)
    msg = {"запущен": "Разбираю, кто есть кто — около минуты, страница обновится сама.",
           "уже идёт": "Разбор уже идёт.", "некого разбирать": "Все говорящие узнаны.",
           "Claude не подключён": "Claude не подключён — подключите его в Стенографе."}.get(res, res)
    return RedirectResponse(f"/calls/{call_id}?msg=" + quote(msg) + "#who", status_code=303)


@app.post("/calls/{call_id}/digest")
def call_digest_start(call_id: str, user: sqlite3.Row = Depends(current_user),
                      conn: sqlite3.Connection = Depends(db)):
    """Разобрать встречу по участникам — один запрос Claude на всех (в фоне)."""
    from urllib.parse import quote
    require(access.sees_all_profiles(user["role"]))
    res = call_digest.start(conn, call_id)
    store.log(conn, user, "запустил разбор созвона по участникам", call_id, res)
    msg = res or "Разбираю встречу по участникам — пара минут, страница обновится сама."
    return RedirectResponse(f"/calls/{call_id}?msg=" + quote(msg) + "#people", status_code=303)


@app.post("/calls/{call_id}/guess/decide")
def call_guess_decide(call_id: str, label: str = Form(...), accept: str = Form("0"),
                      everywhere: str = Form("0"), user: sqlite3.Row = Depends(current_user),
                      conn: sqlite3.Connection = Depends(db)):
    from urllib.parse import quote
    require(access.sees_all_profiles(user["role"]))
    try:
        call_guess.decide(conn, call_id, label, accept == "1", user["login"], everywhere == "1")
    except ValueError:
        raise HTTPException(status_code=404, detail="Подсказки нет.")
    store.log(conn, user, "подтвердил говорящего" if accept == "1" else "отклонил подсказку", label)
    return RedirectResponse(f"/calls/{call_id}?msg=" + quote(
        f"«{label}» отмечен." if accept == "1" else f"Подсказку для «{label}» отклонили.") + "#who",
        status_code=303)


# ------------------------------------------------------------------ тариф

@app.get("/extended", response_class=HTMLResponse)
def extended(request: Request, user: sqlite3.Row = Depends(current_user)):
    require(access.extended(user["role"]))
    return page(request, "extended.html", user, {})


# ------------------------------------------------------------------ админ

def _staff_page(request: Request, user: sqlite3.Row, conn: sqlite3.Connection,
                **extra: Any) -> HTMLResponse:
    ctx = {"s": staff.listing(conn), "roles": staff.STAFF_ROLES, "is_admin": access.is_admin(user["role"]),
           "msg": request.query_params.get("msg"), "err": request.query_params.get("err"),
           "issued": [], "may": lambda role: staff.may_manage(user, role)}
    ctx.update(extra)
    return page(request, "admin_users.html", user, ctx)


def _staff_back(msg: str = "", err: str = "") -> RedirectResponse:
    from urllib.parse import urlencode
    q = {k: v for k, v in (("msg", msg), ("err", err)) if v}
    return RedirectResponse("/admin/users" + ("?" + urlencode(q) if q else ""), status_code=303)


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request, user: sqlite3.Row = Depends(current_user),
                conn: sqlite3.Connection = Depends(db)):
    require(access.manages_people(user["role"]))
    return _staff_page(request, user, conn)


@app.post("/admin/users", response_class=HTMLResponse)
def admin_user_create(request: Request, login: str = Form(""), name: str = Form(""),
                      role: str = Form("mentor"), user: sqlite3.Row = Depends(current_user),
                      conn: sqlite3.Connection = Depends(db)):
    require(access.manages_people(user["role"]))
    try:
        made = staff.create(conn, user, name, login, role)
    except staff.StaffError as e:
        return _staff_page(request, user, conn, err=str(e), form={"name": name, "login": login, "role": role})
    store.log(conn, user, "создал пользователя", made["login"], role)
    return _staff_page(request, user, conn, issued=[made], msg=f"{made['name']} заведён(а).")


@app.post("/admin/users/invite", response_class=HTMLResponse)
def admin_users_invite(request: Request, uid: list[str] = Form(default=[]),
                       user: sqlite3.Row = Depends(current_user),
                       conn: sqlite3.Connection = Depends(db)):
    """Новые пароли выбранным, кто ещё не входил, — по сообщению на каждого."""
    require(access.manages_people(user["role"]))
    issued = []
    for v in uid:
        if v.isdigit():
            try:
                issued.append(staff.reset_password(conn, user, int(v)))
            except staff.StaffError:
                continue
    store.log(conn, user, "выдал доступы", f"{len(issued)} чел.")
    return _staff_page(request, user, conn, issued=issued,
                       msg=f"Новые пароли: {len(issued)}. Перешлите каждому его сообщение.")


@app.post("/admin/users/{uid}", response_class=HTMLResponse)
def admin_user_edit(request: Request, uid: int, name: str = Form(""), login: str = Form(""),
                    role: str = Form(""), user: sqlite3.Row = Depends(current_user),
                    conn: sqlite3.Connection = Depends(db)):
    require(access.manages_people(user["role"]))
    try:
        staff.update(conn, user, uid, name, login, role)
    except staff.StaffError as e:
        return _staff_back(err=str(e))
    store.log(conn, user, "изменил пользователя", login, role)
    return _staff_back(f"Сохранено: {name}.")


@app.post("/admin/users/{uid}/password", response_class=HTMLResponse)
def admin_user_password(request: Request, uid: int, user: sqlite3.Row = Depends(current_user),
                        conn: sqlite3.Connection = Depends(db)):
    require(access.manages_people(user["role"]))
    try:
        made = staff.reset_password(conn, user, uid)
    except staff.StaffError as e:
        return _staff_back(err=str(e))
    store.log(conn, user, "выдал новый пароль", made["login"])
    return _staff_page(request, user, conn, issued=[made],
                       msg=f"Новый пароль для {made['name']}. Старый больше не действует.")


@app.post("/admin/users/{uid}/active")
def admin_user_active(uid: int, active: str = Form("0"),
                      user: sqlite3.Row = Depends(current_user),
                      conn: sqlite3.Connection = Depends(db)):
    require(access.manages_people(user["role"]))
    try:
        staff.set_active(conn, user, uid, active == "1")
    except staff.StaffError as e:
        return _staff_back(err=str(e))
    store.log(conn, user, "изменил доступ", f"пользователь {uid}",
              "включён" if active == "1" else "отключён")
    return _staff_back("Доступ включён." if active == "1" else "Доступ отключён, открытые входы закрыты.")


# ------------------------------------------------------------------ настройки

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: sqlite3.Row = Depends(current_user)):
    return page(request, "settings.html", user, {
        "msg": request.query_params.get("msg"), "err": request.query_params.get("err")})


@app.post("/settings/motion")
def settings_motion(motion: str = Form("auto"), user: sqlite3.Row = Depends(current_user),
                    conn: sqlite3.Connection = Depends(db)):
    if motion not in ("on", "off", "auto"):
        motion = "auto"
    store.set_setting(conn, f"ui:motion:{user['id']}", motion)
    return RedirectResponse("/settings?msg=" + __import__("urllib.parse").parse.quote("Сохранено."),
                            status_code=303)


@app.post("/settings/password")
def settings_password(old: str = Form(""), new: str = Form(""), again: str = Form(""),
                      user: sqlite3.Row = Depends(current_user),
                      conn: sqlite3.Connection = Depends(db)):
    from urllib.parse import quote
    if not store.check_password(old, user["salt"], user["pwhash"]):
        return RedirectResponse("/settings?err=" + quote("Текущий пароль не подошёл."), status_code=303)
    if len(new) < 8:
        return RedirectResponse("/settings?err=" + quote("Новый пароль — не короче 8 символов."), status_code=303)
    if new != again:
        return RedirectResponse("/settings?err=" + quote("Новый пароль и повтор не совпадают."), status_code=303)
    store.set_password(conn, user["id"], new)
    store.log(conn, user, "сменил свой пароль")
    return RedirectResponse("/settings?msg=" + quote("Пароль сменён."), status_code=303)


# ------------------------------------------------------------------ потоки

def _flow_or_404(conn: sqlite3.Connection, fid: int) -> sqlite3.Row:
    f = flows.get(conn, fid)
    if not f:
        raise HTTPException(status_code=404, detail="Поток не найден.")
    return f


def _native_people(conn: sqlite3.Connection, f) -> dict[int, list[dict]]:
    """Участники команд, которые ведутся на платформе, — по командам потока."""
    out: dict[int, list[dict]] = {}
    for t in flows.teams(conn, f["id"]):
        if t["sheet_id"]:
            continue
        out[t["id"]] = [dict(p, card=roster.card_id(p["id"])) for p in conn.execute(
            "SELECT p.* FROM people p JOIN teams g ON g.id=p.team_id WHERE p.flow=? AND g.flow=? AND g.title=? "
            "ORDER BY p.status<>'в потоке', p.name", (f["name"], f["name"], t["title"]))]
    return out


def _flow_page(request: Request, user: sqlite3.Row, conn: sqlite3.Connection, fid: int,
               **extra: Any) -> HTMLResponse:
    f = _flow_or_404(conn, fid)
    staff = conn.execute(
        "SELECT id, login, name, role FROM users WHERE active=1 AND role IN "
        "('mentor','coordinator','admin') ORDER BY role='mentor' DESC, name").fetchall()
    # Аккаунт сообщества: вход и выбор чатов — прямо здесь, на странице потока
    tg_st = tg_account.status() if access.manages_telegram(user["role"]) else {"ok": False}
    tg = tg_account.chats() if tg_st.get("connected") else {}
    flows.remap_migrated(conn, tg.get("migrated"))
    # ключ — (чат, тема); тема 0 — вся группа
    bound = {(c["chat_id"], c["topic_id"]): c for c in flows.chats(conn, fid)}
    bound_chats = {k[0] for k in bound}
    elsewhere = flows.chat_elsewhere(conn, fid)
    # сверху выбранные, потом — по свежести переписки
    tg_chats = sorted(tg.get("chats") or [],
                      key=lambda c: (c["id"] not in bound_chats, -(_ts(c.get("last")))))
    ctx = {
        "f": f, "teams": flows.teams(conn, fid), "staff": staff, "native_people": _native_people(conn, f),
        "fourday": fourday.is_fourday(f),
        # отбор в основной ММ — у потока с таблицей участников 4 дней (ММ8)
        "pick": selection.summary(conn, f) if fourday.sheet_of(conn, fid) else None,
        "fd_people": fourday.participants(conn, f) if fourday.is_fourday(f) else [],
        "fd_sheet": fourday.sheet_of(conn, fid) if fourday.is_fourday(f) else "",
        "fd_targets": [dict(t, flow=x["name"]) for x in flows.listing(conn)
                       if x["status"] != "завершён" and not fourday.is_fourday(x) for t in flows.teams(conn, x["id"])]
                      if fourday.is_fourday(f) else [],
        "join_link": join.flow_link(conn, fid), "join_pending": join.pending(conn, fid),
        "join_invites": join.invites(conn, fid),
        "join_done": join.decided(conn, fid), "join_ready": join.configured(conn),
        "personal_links": {r["person_id"]: r["token"] for r in conn.execute(
            "SELECT person_id, token FROM join_links WHERE flow_id=? AND person_id IS NOT NULL AND revoked_at IS NULL "
            "AND used_at IS NULL", (fid,))},
        # за обратным прокси (Caddy) адрес снаружи — https и внешний хост
        "base_url": f"{request.headers.get('x-forwarded-proto', request.url.scheme)}://"
                    f"{request.headers.get('host', request.url.netloc)}",
        "sync": roster.last_sync(conn, f["name"]),
        "people": conn.execute("SELECT status, COUNT(*) n FROM people WHERE flow=? GROUP BY status",
                               (f["name"],)).fetchall(),
        "rep_chats": [{"c": c, "read": conn.execute("SELECT * FROM chat_reads WHERE chat_id=?", (c["chat_id"],)).fetchone()}
                      for c in reports.report_chats(conn, fid)],
        "rep_strangers": reports.strangers(conn, f) if reports.report_chats(conn, fid) else [],
        "rep_people": [{"card": p["card"], "name": p["name"]} for p in roster.roster(conn, f["name"])
                       if p["status"] == "в потоке"],
        "tg_st": tg_st, "tg_at": tg.get("at"), "tg_chats": tg_chats, "bound": bound,
        "elsewhere": elsewhere, "chat_kinds": flows.CHAT_KINDS, "flow_chats": list(bound.values()),
        "msg": request.query_params.get("msg"), "err": request.query_params.get("err"),
        "check": None, "form": {}, "new_user": None,
        "disk": (disk := ydisk.items(conn, fid)), "disk_stats": ydisk.stats(disk),
        "disk_people": ydisk.people_of(conn, f),
    }
    ctx.update(extra)
    return page(request, "flow_edit.html", user, ctx)


def _ts(iso: str | None) -> float:
    from datetime import datetime as _dt
    try:
        return _dt.fromisoformat(iso).timestamp() if iso else 0.0
    except ValueError:
        return 0.0


def _back(fid: int, msg: str = "", err: str = "") -> RedirectResponse:
    from urllib.parse import urlencode
    q = {k: v for k, v in (("msg", msg), ("err", err)) if v}
    return RedirectResponse(f"/flows/{fid}" + ("?" + urlencode(q) if q else ""), status_code=303)


def _ids(values: list[str]) -> list[int]:
    return [int(v) for v in values if str(v).strip().isdigit()]


@app.get("/flows", response_class=HTMLResponse)
def flows_page(request: Request, user: sqlite3.Row = Depends(current_user),
               conn: sqlite3.Connection = Depends(db)):
    require(access.manages_flows(user["role"]))
    return page(request, "flows.html", user, {
        "flows": flows.listing(conn), "err": request.query_params.get("err")})


@app.post("/flows")
def flow_create(name: str = Form(""), start: str = Form(""), finish: str = Form(""), kind: str = Form("поток"),
                user: sqlite3.Row = Depends(current_user),
                conn: sqlite3.Connection = Depends(db)):
    require(access.manages_flows(user["role"]))
    from urllib.parse import quote
    try:
        fid = flows.create(conn, name, start, finish, kind)
    except flows.FlowError as e:
        return RedirectResponse("/flows?err=" + quote(str(e)), status_code=303)
    store.log(conn, user, "создал поток", name)
    if kind == fourday.KIND:
        return _back(fid, "4-дневный мастермайнд создан. Участники заведутся сами из таблицы участников — "
                          "проверьте ссылку на неё ниже и нажмите «Обновить из таблицы».")
    return _back(fid, "Поток создан. Добавьте команды — по таблице деклараций на каждую.")


@app.get("/flows/{fid}", response_class=HTMLResponse)
def flow_edit(request: Request, fid: int, user: sqlite3.Row = Depends(current_user),
              conn: sqlite3.Connection = Depends(db)):
    require(access.manages_flows(user["role"]))
    return _flow_page(request, user, conn, fid)


@app.post("/flows/{fid}/settings")
def flow_settings(fid: int, name: str = Form(""), start: str = Form(""), finish: str = Form(""),
                  note: str = Form(""), user: sqlite3.Row = Depends(current_user),
                  conn: sqlite3.Connection = Depends(db)):
    require(access.manages_flows(user["role"]))
    try:
        flows.update(conn, fid, name, start, finish, note)
    except flows.FlowError as e:
        return _back(fid, err=str(e))
    store.log(conn, user, "изменил поток", name)
    return _back(fid, "Сохранено.")


@app.post("/flows/{fid}/state")
def flow_state(fid: int, action: str = Form(""), user: sqlite3.Row = Depends(current_user),
               conn: sqlite3.Connection = Depends(db)):
    require(access.manages_flows(user["role"]))
    f = _flow_or_404(conn, fid)
    try:
        if action == "current":
            flows.make_current(conn, fid)
            msg = f"«{f['name']}» теперь текущий поток: его показывают «Обзор», «Поток» и «Участники»."
        elif action == "finish":
            flows.set_status(conn, fid, "завершён")
            msg = "Поток завершён. Таблицы больше не читаются, данные остались в архиве."
        elif action == "reopen":
            flows.set_status(conn, fid, "идёт")
            msg = "Поток снова в работе."
        elif action == "prepare":
            flows.set_status(conn, fid, "готовится")
            msg = "Поток помечен как готовящийся."
        else:
            return _back(fid)
    except flows.FlowError as e:
        return _back(fid, err=str(e))
    store.log(conn, user, "поток: " + action, f["name"])
    return _back(fid, msg)


@app.post("/flows/{fid}/delete")
def flow_delete(fid: int, user: sqlite3.Row = Depends(current_user),
                conn: sqlite3.Connection = Depends(db)):
    require(access.manages_flows(user["role"]))
    f = _flow_or_404(conn, fid)
    try:
        flows.delete(conn, fid)
    except flows.FlowError as e:
        return _back(fid, err=str(e))
    store.log(conn, user, "удалил поток", f["name"])
    return RedirectResponse("/flows", status_code=303)


@app.post("/flows/{fid}/sync")
def flow_sync_now(fid: int, user: sqlite3.Row = Depends(current_user),
                  conn: sqlite3.Connection = Depends(db)):
    """Перечитать таблицы сейчас, не дожидаясь часа. Идёт в фоне: четыре
    таблицы читаются около минуты."""
    require(access.manages_flows(user["role"]))
    f = _flow_or_404(conn, fid)
    if f["status"] == "завершён":
        return _back(fid, err="Поток завершён — его таблицы больше не читаются.")
    name = f["name"]

    def run():
        c = store.connect()
        try:
            roster.sync_flow(c, roster.config(c, name))
        finally:
            c.close()
    __import__("threading").Thread(target=run, daemon=True).start()
    store.log(conn, user, "обновил таблицы потока", name)
    return _back(fid, "Таблицы обновляются — через минуту обновите страницу.")


@app.post("/flows/{fid}/teams", response_class=HTMLResponse)
def flow_team_add(request: Request, fid: int, action: str = Form("check"),
                  title: str = Form(""), link: str = Form(""), no: str = Form(""),
                  mentors: list[str] = Form(default=[]), trackers: list[str] = Form(default=[]),
                  user: sqlite3.Row = Depends(current_user),
                  conn: sqlite3.Connection = Depends(db)):
    """Сначала «Проверить» — платформа читает таблицу и показывает, кого нашла;
    потом «Добавить». Добавить можно и без проверки, но с предупреждением."""
    require(access.manages_flows(user["role"]))
    form = {"title": title, "link": link, "no": no,
            "mentors": _ids(mentors), "trackers": _ids(trackers)}
    sid = flows.sheet_id_of(link) if link.strip() else ""
    if sid is None:
        return _flow_page(request, user, conn, fid, form=form,
                          err="Не похоже на ссылку на Google-таблицу. Скопируйте адрес из строки браузера "
                              "или оставьте поле пустым — тогда команда ведётся на платформе.")
    if action == "check" and sid:
        return _flow_page(request, user, conn, fid, form=form, check=roster.check_sheet(sid))
    try:
        flows.add_team(conn, fid, title, link, form["mentors"], form["trackers"], as_int(no))
    except flows.FlowError as e:
        return _flow_page(request, user, conn, fid, form=form, err=str(e))
    store.log(conn, user, "добавил команду в поток", title)
    return _back(fid, f"Команда «{title}» добавлена. " + ("Участники появятся после обновления таблиц." if sid
                      else "Она ведётся на платформе — добавьте участников в её карточке ниже."))


@app.post("/flows/{fid}/teams/{tid}")
def flow_team_edit(fid: int, tid: int, title: str = Form(""), link: str = Form(""),
                   no: str = Form(""), goal: str = Form(""), mentors: list[str] = Form(default=[]),
                   trackers: list[str] = Form(default=[]),
                   user: sqlite3.Row = Depends(current_user),
                   conn: sqlite3.Connection = Depends(db)):
    require(access.manages_flows(user["role"]))
    try:
        flows.update_team(conn, tid, title, link, _ids(mentors), _ids(trackers), as_int(no))
        flows.set_goal(conn, tid, as_int(goal.strip()) if goal.strip() else None)
    except flows.FlowError as e:
        return _back(fid, err=str(e))
    store.log(conn, user, "изменил команду потока", title)
    return _back(fid, "Команда сохранена. Закрепления менторов обновятся вместе с таблицами.")


@app.post("/flows/{fid}/teams/{tid}/remove")
def flow_team_remove(fid: int, tid: int, user: sqlite3.Row = Depends(current_user),
                     conn: sqlite3.Connection = Depends(db)):
    require(access.manages_flows(user["role"]))
    t = conn.execute("SELECT title FROM flow_teams WHERE id=? AND flow_id=?", (tid, fid)).fetchone()
    if t:
        flows.remove_team(conn, tid)
        store.log(conn, user, "убрал команду из потока", t["title"])
    return _back(fid, "Команда убрана из потока.")


@app.post("/flows/{fid}/staff", response_class=HTMLResponse)
def flow_staff_new(request: Request, fid: int, name: str = Form(""), login: str = Form(""),
                   user: sqlite3.Row = Depends(current_user),
                   conn: sqlite3.Connection = Depends(db)):
    """Новый ментор прямо из потока: логин — его телеграм без @, пароль
    придумывает платформа и показывает один раз."""
    require(access.manages_flows(user["role"]))
    import secrets
    login = login.strip().lstrip("@").lower()
    name = re.sub(r"\s+", " ", name).strip()
    if not name or not re.fullmatch(r"[a-z0-9_.]{3,40}", login):
        return _flow_page(request, user, conn, fid,
                          err="Нужны имя и логин: латиница, цифры и «_», например его ник в телеграме.")
    password = secrets.token_urlsafe(9)
    try:
        store.create_user(conn, login, name, "mentor", password)
    except sqlite3.IntegrityError:
        return _flow_page(request, user, conn, fid, err=f"Логин «{login}» уже занят.")
    store.log(conn, user, "создал пользователя", login, "mentor")
    return _flow_page(request, user, conn, fid,
                      new_user={"name": name, "login": login, "password": password})


@app.post("/flows/{fid}/chats")
def flow_chats_save(fid: int, chat: list[str] = Form(default=[]),
                    user: sqlite3.Row = Depends(current_user),
                    conn: sqlite3.Connection = Depends(db)):
    """chat — «<id чата>:<id темы>|<роль>[|<id команды>]», тема 0 — вся группа;
    пустое — не относится."""
    from urllib.parse import quote
    require(access.manages_flows(user["role"]))
    _flow_or_404(conn, fid)
    known = {c["id"]: c for c in (tg_account.chats().get("chats") or [])}
    old = {(c["chat_id"], c["topic_id"]): c for c in flows.chats(conn, fid)}
    picked = []
    for v in chat:
        parts = v.split("|")
        m = re.fullmatch(r"(-?\d+):(\d+)", parts[0]) if len(parts) >= 2 and parts[1] else None
        if not m:
            continue
        cid, tid = int(m.group(1)), int(m.group(2))
        info, was = known.get(cid) or {}, old.get((cid, tid))
        topic = next((t["title"] for t in info.get("topics") or [] if t["id"] == tid), None) \
            or (was["topic_title"] if was else None)
        picked.append({
            "chat_id": cid, "topic_id": tid, "kind": parts[1],
            "title": info.get("title") or (was["title"] if was else "") or "",
            "topic_title": topic,
            "team_id": int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None})
    try:
        flows.set_chats(conn, fid, picked)
    except flows.FlowError as e:
        return _back(fid, err=str(e))
    store.log(conn, user, "выбрал чаты потока", str(fid), f"чатов {len(picked)}")
    return RedirectResponse(f"/flows/{fid}?msg=" + quote(
        f"Чаты потока сохранены: {len(picked)}.") + "#chats", status_code=303)


@app.get("/telegram", response_class=HTMLResponse)
def telegram_page(request: Request, user: sqlite3.Row = Depends(current_user),
                  conn: sqlite3.Connection = Depends(db)):
    require(access.manages_telegram(user["role"]))
    st = tg_account.status()
    return page(request, "telegram.html", user, {
        "st": st, "chats": tg_account.chats() if st.get("connected") else {},
        "send_on": tg_account.sending_allowed(), "send_mode": tg_account.send_mode()[0],
        "login_bot": join.bot(conn)["name"], "login_token_set": bool(join.bot(conn)["token"]),
        "is_admin": access.is_admin(user["role"]),
        "msg": request.query_params.get("msg"), "err": request.query_params.get("err"),
        "need_password": bool(st.get("need_password")),
    })


def _tg_back(res: dict[str, Any], ok_msg: str = "", back: str = "") -> RedirectResponse:
    """Вход в аккаунт идёт и со страницы «Телеграм», и со страницы потока —
    возвращаем туда, откуда пришли (только на свои страницы)."""
    from urllib.parse import urlencode
    path = back if re.fullmatch(r"/flows/\d+", back or "") else "/telegram"
    q = {}
    if res.get("need_password"):
        q["pw"] = "1"
    elif not res.get("ok"):
        q["err"] = res.get("error") or "Не получилось."
    elif ok_msg:
        q["msg"] = ok_msg
    anchor = "#chats" if path != "/telegram" else ""
    return RedirectResponse(path + ("?" + urlencode(q) if q else "") + anchor, status_code=303)


@app.post("/telegram/phone")
def telegram_phone(phone: str = Form(...), back: str = Form(""), user: sqlite3.Row = Depends(current_user),
                   conn: sqlite3.Connection = Depends(db)):
    require(access.manages_telegram(user["role"]))
    res = tg_account.send_code(phone)
    store.log(conn, user, "телеграм: запросил код", phone[-4:].rjust(len(phone), "•"))
    return _tg_back(res, "Аккаунт уже подключён." if res.get("already") else
                    "Код отправлен в приложение Телеграм на этом аккаунте.", back)


@app.post("/telegram/code")
def telegram_code(code: str = Form(""), password: str = Form(""), back: str = Form(""),
                  user: sqlite3.Row = Depends(current_user),
                  conn: sqlite3.Connection = Depends(db)):
    require(access.manages_telegram(user["role"]))
    res = tg_account.sign_in(code, password)
    if res.get("ok"):
        store.log(conn, user, "телеграм: подключил аккаунт", res.get("name") or "")
    return _tg_back(res, "Аккаунт подключён — теперь выберите чаты потока." if back
                    else "Аккаунт подключён.", back)


@app.post("/telegram/chats")
def telegram_chats(back: str = Form(""), user: sqlite3.Row = Depends(current_user)):
    require(access.manages_telegram(user["role"]))
    return _tg_back(tg_account.refresh_chats(), "Список чатов обновлён.", back)


@app.post("/telegram/send-lock")
def telegram_send_lock(on: str = Form("0"), mode: str = Form(""), user: sqlite3.Row = Depends(current_user),
                       conn: sqlite3.Connection = Depends(db)):
    """Замок на отправку в телеграм: закрыт / только тестовый чат / везде.
    Переключает только администратор."""
    require(access.is_admin(user["role"]))
    mode = mode if mode in ("off", "test", "on") else ("on" if on == "1" else "off")
    store.set_setting(conn, "tg:send", mode)
    label = {"off": "закрыт", "test": "открыт только для тестового чата", "on": "открыт для всех чатов"}[mode]
    store.log(conn, user, "замок отправки в телеграм", label)
    return _tg_back({"ok": True}, {"off": "Отправка в телеграм выключена.",
                                   "test": "Отправка открыта только в тестовый чат «Бот».",
                                   "on": "Отправка в телеграм включена для всех чатов."}[mode])


@app.post("/telegram/logout")
def telegram_logout(back: str = Form(""), user: sqlite3.Row = Depends(current_user),
                    conn: sqlite3.Connection = Depends(db)):
    require(access.manages_telegram(user["role"]))
    store.log(conn, user, "телеграм: отключил аккаунт")
    return _tg_back(tg_account.logout(), "Аккаунт отключён от платформы.", back)


# ------------------------------------------------------------------ посты

def _posts_back(flow_id: int, msg: str = "", err: str = "", anchor: str = "",
                chat: int | None = None, topic: int = 0) -> RedirectResponse:
    from urllib.parse import urlencode
    q = {"flow": flow_id, **{k: v for k, v in (("msg", msg), ("err", err)) if v}}
    if chat:
        q.update({"chat": chat, "topic": topic or 0})
    return RedirectResponse("/posts?" + urlencode(q) + (f"#{anchor}" if anchor else ""), status_code=303)


def _organizer_from_cookie(request: Request) -> sqlite3.Row | None:
    """Проверка входа без общей зависимости базы: картинки чата грузятся
    десятками одновременно, и общая зависимость падала («объект из другого потока»)."""
    conn = store.connect()
    try:
        u = store.session_user(conn, request.cookies.get(SESSION_COOKIE))
        return u if u and access.manages_flows(u["role"]) else None
    finally:
        conn.close()


@app.get("/posts", response_class=HTMLResponse)
def posts_page(request: Request, user: sqlite3.Row = Depends(current_user),
               conn: sqlite3.Connection = Depends(db)):
    """«Посты» — телеграм внутри платформы: чаты потока по ролям, переписка,
    черновики, примеры для ИИ. Только организаторы."""
    require(access.manages_flows(user["role"]))
    live = [f for f in flows.listing(conn) if f["status"] != "завершён"]
    want = request.query_params.get("flow", "")
    cur = flows.current(conn)
    f = next((x for x in live if str(x["id"]) == want), None) \
        or next((x for x in live if cur and x["id"] == cur["id"]), None) or (live[0] if live else None)
    st = tg_account.status() if f else {}
    groups = chatui.chat_list(conn, f["id"]) if f else []
    chat = as_int(request.query_params.get("chat", ""))
    topic = as_int(request.query_params.get("topic", "")) or 0
    allc = [c for g in groups for c in g["chats"]]
    sel = next((c for c in allc if c["chat_id"] == chat and c["topic_id"] == topic), None) \
        or next((c for c in allc if c["chat_id"] == chat), None)
    msgs = chatui.messages(conn, sel["chat_id"], sel["topic_id"]) if sel else []
    drafts = [dict(p, html=emoji.preview_html(p["text"] or ""), files=posts.media_of(p))
              for p in (posts.listing(conn, f["id"]) if f else []) if p["status"] in ("черновик", "пишется")]
    ex = [dict(e, html=emoji.preview_html(e["text"])) for e in posts.examples(conn)]
    return page(request, "chats.html", user, {
        "flows_live": live, "f": f, "groups": groups, "sel": sel, "msgs": msgs,
        "drafts": drafts, "here": [d for d in drafts if sel and d["chat_id"] == sel["chat_id"]
                                   and (d["topic_id"] or 0) == sel["topic_id"]],
        "examples": ex, "facts": chatui.facts(conn, f) if f else None, "tg": st,
        "send_on": tg_account.sending_allowed(sel["chat_id"] if sel else None), "send_mode": tg_account.send_mode()[0],
        "kinds": posts.KINDS, "gen_kinds": posts.GEN_KINDS,
        "claude_on": bool(claude_env.token()),
        "people": [{"card": p["card"], "name": p["name"]} for p in roster.roster(conn, f["name"])
                   if p["status"] == "в потоке"] if f else [],
        "msg": request.query_params.get("msg"), "err": request.query_params.get("err"),
    })


@app.get("/posts/messages", response_class=HTMLResponse)
def posts_messages(request: Request, chat: int, topic: int = 0, refresh: int = 0,
                   user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(db)):
    """Переписка чата фрагментом — для обновления без перезагрузки. refresh=1 —
    сначала дочитать чат из телеграма (только чтение)."""
    require(access.manages_flows(user["role"]))
    known = {c["chat_id"] for f in flows.listing(conn) for c in flows.chats(conn, f["id"])}
    if chat not in known:
        raise HTTPException(status_code=404, detail="Чат не выбран ни в одном потоке.")
    if refresh:
        chatui.refresh(conn, chat)
    return page(request, "_chat_messages.html", user, {"msgs": chatui.messages(conn, chat, topic), "chat": chat})


@app.get("/posts/older", response_class=HTMLResponse)
def posts_older(request: Request, chat: int, before: int, topic: int = 0,
                user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(db)):
    """Сообщения раньше before — подгрузка при прокрутке вверх (только чтение)."""
    require(access.manages_flows(user["role"]))
    known = {c["chat_id"] for f in flows.listing(conn) for c in flows.chats(conn, f["id"])}
    if chat not in known:
        raise HTTPException(status_code=404, detail="Чат не выбран ни в одном потоке.")
    msgs = chatui.older(conn, chat, topic, before)
    return page(request, "_chat_messages.html", user, {"msgs": msgs, "chat": chat, "older": True})


@app.get("/tgmedia/{name}")
def tg_media(name: str, request: Request):
    from fastapi.responses import FileResponse
    if not _organizer_from_cookie(request):
        raise HTTPException(status_code=403)
    if not re.fullmatch(r"-?\d+_\d+\.jpg", name):
        raise HTTPException(status_code=404)
    p = chatui.MEDIA / name
    if not p.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@app.get("/tgavatar/{uid}")
def tg_avatar(uid: int, request: Request):
    from fastapi.responses import FileResponse
    if not _organizer_from_cookie(request):
        raise HTTPException(status_code=403)
    p = chatui.AVATARS / f"{uid}.jpg"
    if not p.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@app.get("/post_media/{pid}/{name}")
def post_media(pid: int, name: str, request: Request):
    from fastapi.responses import FileResponse
    if not _organizer_from_cookie(request) or not re.fullmatch(r"\d{2}\.(jpe?g|png|webp)", name):
        raise HTTPException(status_code=404)
    p = posts.MEDIA / str(pid) / name
    if not p.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(p)


@app.post("/posts/draft")
def posts_draft(flow: int = Form(...), chat: int = Form(...), topic: int = Form(0), text: str = Form(""),
                reply_to: str = Form(""), files: list[UploadFile] = File(default=[]),
                avatar_card: str = Form(""), user: sqlite3.Row = Depends(current_user),
                conn: sqlite3.Connection = Depends(db)):
    """Черновик прямо в чате: текст, картинки, ответ на сообщение, фото из аватарки.
    Ничего не отправляет — только сохраняет."""
    require(access.manages_flows(user["role"]))
    try:
        pid = posts.create_in_chat(conn, flow, chat, topic, text, user, as_int(reply_to))
        for up in files or []:
            if up and up.filename:
                posts.add_media(conn, pid, up.filename, up.file.read())
        if avatar_card.strip():
            posts.add_avatar(conn, pid, int(avatar_card))
    except (posts.PostError, ValueError) as e:
        return _posts_back(flow, err=str(e), chat=chat, topic=topic)
    store.log(conn, user, "написал черновик в чате", str(chat))
    return _posts_back(flow, "Черновик сохранён — он внизу чата. Отправка — кнопкой.", anchor=f"post-{pid}",
                       chat=chat, topic=topic)


@app.post("/posts/{pid}/media")
def posts_media(pid: int, files: list[UploadFile] = File(default=[]), avatar_card: str = Form(""),
                drop: str = Form(""), user: sqlite3.Row = Depends(current_user),
                conn: sqlite3.Connection = Depends(db)):
    """Приложить картинки / фото из аватарки участника к черновику или убрать картинку."""
    require(access.manages_flows(user["role"]))
    p = posts.get(conn, pid)
    if not p:
        raise HTTPException(status_code=404)
    try:
        if drop:
            posts.drop_media(conn, pid, drop)
        for up in files or []:
            if up and up.filename:
                posts.add_media(conn, pid, up.filename, up.file.read())
        if avatar_card.strip():
            posts.add_avatar(conn, pid, int(avatar_card))
    except (posts.PostError, ValueError) as e:
        return _posts_back(p["flow_id"], err=str(e), chat=p["chat_id"], topic=p["topic_id"] or 0)
    return _posts_back(p["flow_id"], "Картинки обновлены.", anchor=f"post-{pid}",
                       chat=p["chat_id"], topic=p["topic_id"] or 0)


@app.post("/posts/generate")
def posts_generate(flow: int = Form(...), kind: str = Form(...), hint: str = Form(""),
                   user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(db)):
    """«✨ Сгенерировать»: Claude пишет пост по фактам платформы в стиле примеров.
    Возвращает текст для поля ввода — ничего не сохраняет и не отправляет."""
    from fastapi.responses import JSONResponse
    require(access.manages_flows(user["role"]))
    try:
        text = posts.generate(conn, flow, kind, hint)
    except posts.PostError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    store.log(conn, user, "сгенерировал пост", posts.GEN_KINDS.get(kind, kind))
    return {"text": text}


@app.post("/posts/new")
def posts_new(flow: int = Form(...), kind: str = Form(...),
              user: sqlite3.Row = Depends(current_user),
              conn: sqlite3.Connection = Depends(db)):
    require(access.manages_flows(user["role"]))
    try:
        pid = posts.create(conn, flow, kind, user)
    except posts.PostError as e:
        return _posts_back(flow, err=str(e))
    store.log(conn, user, "создал черновик поста", posts.KINDS.get(kind, kind))
    return _posts_back(flow, "Черновик собран из таблиц потока — проверьте и поправьте.", anchor=f"post-{pid}")


@app.get("/emoji/{doc_id}")
def emoji_image(doc_id: int):
    """Картинка премиум-эмодзи для предпросмотра постов. Без проверки входа:
    это общедоступные картинки наборов телеграма, а проверка на каждую из
    десятков одновременных картинок спотыкалась о базу (14.10.2026)."""
    from fastapi.responses import FileResponse
    p = emoji.DIR / f"{doc_id}.thumb"
    if not p.is_file():
        raise HTTPException(status_code=404)
    head = p.read_bytes()[:12]
    kind = "image/webp" if head[8:12] == b"WEBP" else "image/png" if head.startswith(b"\x89PNG") else "image/jpeg"
    return FileResponse(p, media_type=kind, headers={"Cache-Control": "max-age=604800"})


@app.post("/posts/examples/{eid}/remove")
def posts_example_remove(eid: int, flow: str = Form(""), user: sqlite3.Row = Depends(current_user),
                         conn: sqlite3.Connection = Depends(db)):
    require(access.manages_flows(user["role"]))
    conn.execute("DELETE FROM post_examples WHERE id=?", (eid,))
    store.log(conn, user, "убрал пример для ИИ", str(eid))
    return _posts_back(as_int(flow) or 0, "Пример убран — ИИ больше на него не опирается.", anchor="examples")


@app.post("/posts/{pid}")
def posts_act(pid: int, action: str = Form("save"), text: str = Form(""), chat: str = Form(""),
              user: sqlite3.Row = Depends(current_user),
              conn: sqlite3.Connection = Depends(db)):
    """Сохранить, переписать с Claude, отправить, удалить черновик, удалить из чата."""
    require(access.manages_flows(user["role"]))
    p = posts.get(conn, pid)
    if not p:
        raise HTTPException(status_code=404, detail="Пост не найден.")
    fid, anchor = p["flow_id"], f"post-{pid}"
    back = {"chat": p["chat_id"], "topic": p["topic_id"] or 0}
    try:
        if action in ("save", "send", "claude"):
            posts.update(conn, pid, text, chat)
        if action == "save":
            return _posts_back(fid, "Черновик сохранён.", anchor=anchor, **back)
        if action == "claude":
            posts.rewrite(pid)
            store.log(conn, user, "отдал пост Claude", str(pid))
            return _posts_back(fid, "Claude переписывает пост — страница обновится сама.", anchor=anchor, **back)
        if action == "send":
            posts.send(conn, pid, user)
            p = posts.get(conn, pid)
            store.log(conn, user, "отправил пост", p["chat_title"] or str(p["chat_id"]), f"сообщение {p['msg_id']}")
            return _posts_back(fid, f"Пост отправлен в «{p['chat_title']}».", anchor=anchor, **back)
        if action == "delete":
            posts.delete(conn, pid)
            return _posts_back(fid, "Черновик удалён.", **back)
        if action == "unsend":
            posts.unsend(conn, pid)
            store.log(conn, user, "удалил пост из чата", p["chat_title"] or "", f"сообщение {p['msg_id']}")
            return _posts_back(fid, "Пост удалён из чата.", anchor=anchor, **back)
    except posts.PostError as e:
        return _posts_back(fid, err=str(e), anchor=anchor, **back)
    raise HTTPException(status_code=400, detail="Неизвестное действие.")


# ------------------------------------------------------------------ отчёты

@app.post("/reports/fetch")
def reports_fetch(flow: str = Form(...), d: str = Form(""), user: sqlite3.Row = Depends(current_user),
                  conn: sqlite3.Connection = Depends(db)):
    """Дочитать чат отчётов сейчас, не дожидаясь утра. Только чтение."""
    from urllib.parse import urlencode
    require(access.sees_all_profiles(user["role"]))
    f = flows.get(conn, name=flow)
    if not f:
        raise HTTPException(status_code=404, detail="Поток не найден.")
    errs, n = [], 0
    for ch in reports.report_chats(conn, f["id"]):
        r = reports.fetch(conn, ch["chat_id"], days=8)
        if r.get("ok"):
            n += r["n"]
        else:
            errs.append(r.get("error") or "не прочиталось")
    store.log(conn, user, "дочитал чат отчётов", flow, f"сообщений {n}")
    q = {"err": "; ".join(errs)} if errs else {"msg": f"Чат отчётов прочитан: {n} сообщений за неделю."}
    return RedirectResponse(f"/flows/{f['id']}?" + urlencode(q) + "#reports", status_code=303)


@app.post("/reports/who")
def reports_who(flow: str = Form(...), d: str = Form(""), sender_id: int = Form(...), card: str = Form(""),
                user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(db)):
    """Отметить, кто из участников пишет с этого аккаунта (или что это не участник)."""
    from urllib.parse import urlencode
    require(access.sees_all_profiles(user["role"]))
    reports.set_sender(conn, sender_id, as_int(card) if card not in ("", "-") else None, user["login"])
    store.log(conn, user, "отметил автора отчётов", str(sender_id), card or "не участник")
    f = flows.get(conn, name=flow)
    return RedirectResponse(f"/flows/{f['id'] if f else ''}?" + urlencode({"msg": "Автор отчётов отмечен."}) + "#reports",
                            status_code=303)


@app.get("/admin/log", response_class=HTMLResponse)
def admin_log(request: Request, user: sqlite3.Row = Depends(current_user),
              conn: sqlite3.Connection = Depends(db)):
    require(access.is_admin(user["role"]))
    return page(request, "admin_log.html", user, {
        "rows": store.recent_log(conn, 300), "stats": store.log_stats(conn)})


# ------------------------------------------------------------------ отбор в основной ММ

@app.get("/selection")
def selection_old(user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(db)):
    """Старый адрес раздела «Отбор» — теперь отбор внутри потока."""
    require(access.manages_flows(user["role"]))
    fl = selection.roster_flows(conn)
    return RedirectResponse(f"/flows/{fl[0]['id']}/selection" if fl else "/flows", status_code=303)


@app.get("/flows/{fid}/selection", response_class=HTMLResponse)
def selection_page(request: Request, fid: int, user: sqlite3.Row = Depends(current_user),
                   conn: sqlite3.Connection = Depends(db)):
    """Кто из 4-дневного мастермайнда идёт на основной ММ — доска внутри потока."""
    require(access.manages_flows(user["role"]))
    f = _flow_or_404(conn, fid)
    if not fourday.sheet_of(conn, fid):
        return RedirectResponse(f"/flows/{fid}", status_code=303)
    return page(request, "selection.html", user, {"x": selection.board(conn, f)})


@app.post("/flows/{fid}/selection/{pid}")
def selection_decide(fid: int, pid: int, lane: str = Form(""), user: sqlite3.Row = Depends(current_user),
                     conn: sqlite3.Connection = Depends(db)):
    require(access.manages_flows(user["role"]))
    f = flows.get(conn, fid)
    if not f:
        return JSONResponse({"ok": False, "error": "поток не найден"}, status_code=404)
    try:
        res = selection.decide(conn, f["name"], pid, lane, user["name"] or user["login"])
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    store.log(conn, user, "отбор в основной ММ", "потоки", f"{f['name']}: {pid} → {lane}")
    return JSONResponse(res)


# ------------------------------------------------------------------ сенсационные новости

@app.get("/sensations", response_class=HTMLResponse)
def sensations_page(request: Request, msg: str = "", user: sqlite3.Row = Depends(current_user),
                    conn: sqlite3.Connection = Depends(db)):
    """Оплаты из Тильды → новости о вовлечении и фото чата «вовлекли N»."""
    require(access.manages_flows(user["role"]))
    x = sensation.listing(conn)
    mode, test = tg_account.send_mode()
    chat = int(x["settings"]["sens:chat"])
    return page(request, "sensations.html", user, {
        "msg": msg, "x": x, "mode": mode, "allowed": tg_account.sending_allowed(chat),
        "photo_allowed": tg_account.sending_allowed(int(x["settings"]["sens:photo_chat"])),
        "mentor_allowed": tg_account.sending_allowed(int(x["settings"]["sens:mentor_chat"])),
        "is_admin": access.is_admin(user["role"])})


def _sens_back(msg: str) -> RedirectResponse:
    from urllib.parse import quote
    return RedirectResponse("/sensations?msg=" + quote(msg), status_code=303)


@app.get("/sensations/avatar.jpg")
def sensations_avatar(n: int = 0, g: int = 0, user: sqlite3.Row = Depends(current_user),
                      conn: sqlite3.Connection = Depends(db)):
    require(access.manages_flows(user["role"]))
    from . import sens_avatar
    n = n or int(sensation.setting(conn, "sens:count") or 0) + 1
    g = g or int(sensation.setting(conn, "sens:goal") or 0)
    return Response(sens_avatar.render(max(0, min(n, 9999)), max(0, min(g, 9999))), media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.post("/sensations/settings")
def sensations_settings(on: str = Form("0"), chat: str = Form(""), photo_chat: str = Form(""), avatar: str = Form("0"),
                        count: str = Form(""), goal: str = Form(""), mentor_chat: str = Form(""),
                        mentor_count: str = Form(""), user: sqlite3.Row = Depends(current_user),
                        conn: sqlite3.Connection = Depends(db)):
    require(access.manages_flows(user["role"]))
    vals = {"sens:on": "1" if on == "1" else "0", "sens:avatar": "1" if avatar == "1" else "0"}
    for key, v in (("sens:chat", chat), ("sens:photo_chat", photo_chat), ("sens:mentor_chat", mentor_chat)):
        if v.strip().lstrip("-").isdigit():
            vals[key] = v.strip()
    for key, v in (("sens:count", count), ("sens:goal", goal), ("sens:mentor_count", mentor_count)):
        if v.strip().isdigit():
            vals[key] = v.strip()
    for k, v in vals.items():
        store.set_setting(conn, k, v)
    store.log(conn, user, "настроил сенсационные новости", "посты", json.dumps(vals, ensure_ascii=False))
    return _sens_back("Сохранено.")


@app.post("/sensations/allow")
def sensations_allow(user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(db)):
    """Пустить чаты новостей в тестовом положении замка (только администратор)."""
    require(access.is_admin(user["role"]))
    raw = store.get_setting(conn, "tg:test_chats", tg_account.TEST_CHATS_DEFAULT) or ""
    have = [x for x in raw.replace(",", " ").split() if x.lstrip("-").isdigit()]
    for key in ("sens:chat", "sens:photo_chat", "sens:mentor_chat"):
        v = sensation.setting(conn, key)
        if v not in have:
            have.append(v)
    store.set_setting(conn, "tg:test_chats", " ".join(have))
    store.log(conn, user, "открыл замок для чатов новостей", "телеграм", " ".join(have))
    return _sens_back("Чаты новостей открыты в тестовом положении замка.")


@app.post("/sensations/refresh")
def sensations_refresh(user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(db)):
    require(access.manages_flows(user["role"]))
    try:
        n = sensation.poll_tilda(conn)
        k = sensation.poll_bank(conn)
    except Exception as exc:              # noqa: BLE001
        return _sens_back(f"Телеграм не ответил: {exc}")
    return _sens_back(f"Новых заказов: {n}, оплат по безналу: {k}.")


@app.post("/sensations/{oid}")
def sensations_decide(oid: int, act: str = Form(""), pid: str = Form(""), user: sqlite3.Row = Depends(current_user),
                      conn: sqlite3.Connection = Depends(db)):
    require(access.manages_flows(user["role"]))
    res = sensation.decide(conn, oid, act, pid, user["login"])
    store.log(conn, user, "сенсационная новость", "посты", f"{oid}: {act} {pid} — {res}")
    return _sens_back(res)


@app.get("/admin/finance", response_class=HTMLResponse)
def admin_finance(request: Request, tab: str = "sum", month: str = "", kind: str = "", project: str = "",
                  article: str = "", status: str = "", wallet: str = "", q: str = "", msg: str = "",
                  user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(db)):
    """Финансы из чата с чеками: сводка, ДДС, разбор, не оплачено, кошельки."""
    require(access.sees_finance(user["role"]))
    if tab not in ("sum", "ask", "wait", "ops", "wallets", "dict"):
        tab = "sum"
    flt = {"kind": kind, "project": project, "article": article, "status": status, "wallet": wallet,
           "q": q.strip(), "month": month}
    ctx: dict[str, Any] = {
        "tab": tab, "month": month, "f": flt, "msg": msg, "d": finance.dictionary(conn),
        "chats": finance.chats(conn), "run": finance.last_run(conn), "running": finance.is_running(conn),
        "months": finance.months(conn), "s": finance.summary(conn, month or None),
        "back": str(request.url.path) + ("?" + str(request.url.query) if request.url.query else ""),
    }
    if tab == "ops":
        ctx["items"] = finance.ops(conn, **flt)
        idx = finance.cards_by_name(conn)
        ctx["cards"] = {o["id"]: finance.card_of(idx, o["participant"]) for o in ctx["items"] if o["participant"]}
    if tab == "wallets":
        ctx["wallets"], ctx["nowallet"] = finance.wallets(conn)
    return page(request, "finance.html", user, ctx)


def _fin_back(back: str, msg: str = "") -> RedirectResponse:
    from urllib.parse import quote
    back = back if back.startswith("/admin/finance") else "/admin/finance"
    if msg:
        back += ("&" if "?" in back else "?") + "msg=" + quote(msg)
    return RedirectResponse(back, status_code=303)


@app.post("/admin/finance/run")
def admin_finance_run(user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(db)):
    require(access.sees_finance(user["role"]))
    res = finance.start(conn)
    store.log(conn, user, "запустил разбор финансов", "", res)
    return _fin_back("/admin/finance", res or "Дочитываю чат и разбираю новое — страница обновится сама.")


def _fin_fields(kind: str, amount: str, status: str, project: str, subproject: str, article: str,
                wallet: str, purpose: str, counterparty: str, participant: str, engaged_by: str,
                note: str) -> dict:
    return {"kind": kind, "amount": amount, "status": status, "project": project, "subproject": subproject,
            "article": article, "wallet": wallet, "purpose": purpose, "counterparty": counterparty,
            "participant": participant, "engaged_by": engaged_by, "note": note}


@app.post("/admin/finance/op/new")
def admin_finance_add(kind: str = Form("расход"), amount: str = Form(""), status: str = Form("оплачено"),
                      project: str = Form(""), subproject: str = Form(""), article: str = Form(""),
                      wallet: str = Form(""), purpose: str = Form(""), counterparty: str = Form(""),
                      participant: str = Form(""), engaged_by: str = Form(""), note: str = Form(""),
                      back: str = Form(""), user: sqlite3.Row = Depends(current_user),
                      conn: sqlite3.Connection = Depends(db)):
    require(access.sees_finance(user["role"]))
    try:
        oid = finance.add(conn, _fin_fields(kind, amount, status, project, subproject, article, wallet, purpose,
                                            counterparty, participant, engaged_by, note), user["login"])
    except ValueError as e:
        return _fin_back(back, str(e))
    store.log(conn, user, "добавил операцию", "финансы", f"операция {oid}")
    return _fin_back(back, "Операция добавлена.")


@app.get("/admin/finance/op/{op_id}", response_class=HTMLResponse)
def admin_finance_op(op_id: int, request: Request, back: str = "", user: sqlite3.Row = Depends(current_user),
                     conn: sqlite3.Connection = Depends(db)):
    """Одна операция: что было в чате и форма правки."""
    require(access.sees_finance(user["role"]))
    op = next(iter(finance.ops(conn, id=op_id)), None)
    if not op:
        raise HTTPException(status_code=404, detail="Операции нет.")
    src = conn.execute("SELECT * FROM chat_messages WHERE chat_id=? AND msg_id=?",
                       (op["chat_id"], op["msg_id"])).fetchone() if op["chat_id"] else None
    return page(request, "finance.html", user, {
        "tab": "edit", "op": op, "src": src, "d": finance.dictionary(conn), "chats": finance.chats(conn),
        "back": back if back.startswith("/admin/finance") else "/admin/finance?tab=ops",
        "run": finance.last_run(conn), "running": finance.is_running(conn), "s": finance.summary(conn),
        "months": [], "month": "", "f": {}, "msg": request.query_params.get("msg", "")})


@app.post("/admin/finance/op/{op_id}")
def admin_finance_edit(op_id: int, kind: str = Form("расход"), amount: str = Form(""), status: str = Form("заявка"),
                       project: str = Form(""), subproject: str = Form(""), article: str = Form(""),
                       wallet: str = Form(""), purpose: str = Form(""), counterparty: str = Form(""),
                       participant: str = Form(""), engaged_by: str = Form(""), note: str = Form(""),
                       back: str = Form(""), user: sqlite3.Row = Depends(current_user),
                       conn: sqlite3.Connection = Depends(db)):
    require(access.sees_finance(user["role"]))
    try:
        finance.edit(conn, op_id, _fin_fields(kind, amount, status, project, subproject, article, wallet, purpose,
                                              counterparty, participant, engaged_by, note), user["login"])
    except ValueError as e:
        from urllib.parse import quote_plus
        return _fin_back(f"/admin/finance/op/{op_id}?back=" + quote_plus(back), str(e))
    store.log(conn, user, "поправил операцию", "финансы", f"операция {op_id}")
    return _fin_back(back, "Сохранено.")


@app.post("/admin/finance/op/{op_id}/pick")
def admin_finance_pick(op_id: int, field: str = Form(...), value: str = Form(...), back: str = Form(""),
                       user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(db)):
    require(access.sees_finance(user["role"]))
    try:
        finance.pick(conn, op_id, field, value, user["login"])
    except ValueError as e:
        return _fin_back(back, str(e))
    store.log(conn, user, "ответил на вопрос по операции", "финансы", f"операция {op_id}: {field}={value}")
    return _fin_back(back)


@app.post("/admin/finance/op/{op_id}/paid")
def admin_finance_paid(op_id: int, wallet: str = Form(""), back: str = Form(""),
                       user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(db)):
    require(access.sees_finance(user["role"]))
    try:
        f = {"status": "оплачено"}
        if wallet:
            f["wallet"] = wallet
        finance.edit(conn, op_id, f, user["login"])
    except ValueError as e:
        return _fin_back(back, str(e))
    store.log(conn, user, "отметил оплату", "финансы", f"операция {op_id}")
    return _fin_back(back, "Отмечено как оплаченное.")


@app.post("/admin/finance/dict")
def admin_finance_dict(kind: str = Form(...), names: str = Form(""), user: sqlite3.Row = Depends(current_user),
                       conn: sqlite3.Connection = Depends(db)):
    require(access.sees_finance(user["role"]))
    try:
        finance.save_dictionary(conn, kind, names)
    except ValueError as e:
        return _fin_back("/admin/finance?tab=dict", str(e))
    store.log(conn, user, "поправил справочник финансов", kind)
    return _fin_back("/admin/finance?tab=dict", "Справочник сохранён.")


@app.post("/admin/finance/wallet")
def admin_finance_wallet(name: str = Form(...), amount: str = Form("0"), since: str = Form(""),
                         user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(db)):
    require(access.sees_finance(user["role"]))
    finance.set_wallet(conn, name, amount, since)
    store.log(conn, user, "задал начальный остаток", name, amount)
    return _fin_back("/admin/finance?tab=wallets", "Сохранено.")


@app.post("/admin/reindex")
def admin_reindex(user: sqlite3.Row = Depends(current_user),
                  conn: sqlite3.Connection = Depends(db)):
    require(access.is_admin(user["role"]))
    corpus.reset_cache()
    n = corpus.reindex(conn)
    store.log(conn, user, "перестроил индекс", detail=f"записей {n}")
    return RedirectResponse("/admin/log", status_code=303)


# ------------------------------------------------------------------ таблица потока (строки вовлечения)

def _rows_back(url: str, msg: str = "", err: str = "") -> RedirectResponse:
    from urllib.parse import urlencode
    q = {k: v for k, v in (("msg", msg), ("err", err)) if v}
    base, _, frag = url.partition("#")
    sep = "&" if "?" in base else "?"
    return RedirectResponse(base + (sep + urlencode(q) if q else "") + ("#" + frag if frag else ""), status_code=303)


def _row_or_404(conn: sqlite3.Connection, pid: int) -> dict:
    r = flowrows.row(conn, pid)
    if not r:
        raise HTTPException(status_code=404, detail="Строка не найдена.")
    return r


@app.get("/rows", response_class=HTMLResponse)
def rows_page(request: Request, flow: int = 0, tag: str = "", q: str = "",
              user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(db)):
    """Таблица потока. Без потока — ближайший, который готовится, иначе текущий."""
    require(access.manages_flows(user["role"]))
    if not flow:
        f = flowrows.target_flow(conn) or flows.current(conn) or (flowrows.open_flows(conn) or [None])[0]
        return RedirectResponse(f"/rows?flow={f['id']}" if f else "/flows", status_code=303)
    f = _flow_or_404(conn, flow)
    money = access.sees_finance(user["role"])
    data = flowrows.table(conn, f, money, tag.strip(), q.strip())
    return page(request, "rows.html", user, {
        "f": f, "rows": data, "cols": [c for c in flowrows.COLUMNS if money or not c[2]],
        "all_flows": flows.listing(conn), "open_flows": flowrows.open_flows(conn), "target": flowrows.target_flow(conn),
        "inbox": flowrows.inbox(conn), "tags": flowrows.flow_tags(conn, f), "tag": tag, "q": q,
        "inviter_names": sorted(set(flowrows.inviters(conn)[0].values()), key=str.lower),
        "msg": request.query_params.get("msg"), "err": request.query_params.get("err")})


@app.get("/flows/{fid}/rows")
def rows_old_link(fid: int):
    return RedirectResponse(f"/rows?flow={fid}", status_code=303)


@app.post("/flows/{fid}/rows")
def rows_add(fid: int, name: str = Form(""), phone: str = Form(""), username: str = Form(""),
             inviter: str = Form(""), flow_id: int = Form(0),
             user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(db)):
    require(access.manages_flows(user["role"]))
    to = flow_id or fid
    try:
        pid = flowrows.add(conn, to, name, phone, username, inviter, user["name"])
    except flowrows.RowError as e:
        return _rows_back(f"/rows?flow={fid}", err=str(e))
    store.log(conn, user, "добавил строку", name, f"поток {to}")
    return _rows_back(f"/rows?flow={to}#r{pid}", f"Строка «{name.strip()}» добавлена.")


@app.post("/rows/{pid}/cell")
def rows_cell(pid: int, key: str = Form(""), value: str = Form(""),
              user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(db)):
    require(access.manages_flows(user["role"]))
    try:
        v = flowrows.set_cell(conn, pid, key, value, user["name"], access.sees_finance(user["role"]))
    except flowrows.RowError as e:
        return JSONResponse({"ok": False, "err": str(e)}, status_code=400)
    store.log(conn, user, "правка строки", str(pid), key)
    return {"ok": True, "value": v}


@app.post("/rows/{pid}/tags")
def rows_tags(pid: int, tags: str = Form(""), back: str = Form(""),
              user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(db)):
    require(access.manages_flows(user["role"]))
    _row_or_404(conn, pid)
    t = flowrows.set_tags(conn, pid, tags)
    if back == "json":
        return {"ok": True, "tags": t}
    return _rows_back(f"/rows/{pid}", "Теги сохранены.")


@app.post("/rows/{pid}/note")
def rows_note(pid: int, text: str = Form(""), user: sqlite3.Row = Depends(current_user),
              conn: sqlite3.Connection = Depends(db)):
    require(access.manages_flows(user["role"]))
    _row_or_404(conn, pid)
    flowrows.note(conn, pid, text, user["name"])
    return _rows_back(f"/rows/{pid}#notes")


@app.post("/rows/{pid}/note/{nid}/delete")
def rows_note_delete(pid: int, nid: int, user: sqlite3.Row = Depends(current_user),
                     conn: sqlite3.Connection = Depends(db)):
    require(access.manages_flows(user["role"]))
    conn.execute("DELETE FROM row_notes WHERE id=? AND person_id=? AND kind='комментарий' AND author=?",
                 (nid, pid, user["name"]))
    return _rows_back(f"/rows/{pid}#notes")


@app.post("/rows/{pid}/move")
def rows_move(pid: int, flow_id: int = Form(0), user: sqlite3.Row = Depends(current_user),
              conn: sqlite3.Connection = Depends(db)):
    require(access.manages_flows(user["role"]))
    r = _row_or_404(conn, pid)
    try:
        new = flowrows.move(conn, pid, flow_id, user["name"])
    except flowrows.RowError as e:
        return _rows_back(f"/rows/{pid}", err=str(e))
    store.log(conn, user, "перенёс строку", r["p"]["name"], f"{r['p']['flow']} → {flow_id}")
    return _rows_back(f"/rows/{new}", "Перенесено. В прежнем потоке осталась пометка «перенесён».")


@app.post("/rows/{pid}/pay")
def rows_pay(pid: int, amount: str = Form(""), paid_on: str = Form(""), text: str = Form(""),
             user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(db)):
    require(access.manages_flows(user["role"]))
    _row_or_404(conn, pid)
    try:
        flowrows.add_pay(conn, pid, amount, paid_on, text, user["name"])
    except flowrows.RowError as e:
        return _rows_back(f"/rows/{pid}", err=str(e))
    store.log(conn, user, "внёс оплату", str(pid), amount)
    return _rows_back(f"/rows/{pid}#pays", "Оплата внесена.")


@app.post("/rows/inbox/{pay_id}")
def rows_inbox(pay_id: int, action: str = Form(""), flow_id: int = Form(0), back: int = Form(0), why: str = Form(""),
               user: sqlite3.Row = Depends(current_user), conn: sqlite3.Connection = Depends(db)):
    require(access.manages_flows(user["role"]))
    url = f"/rows?flow={back}#inbox" if back else "/rows"
    try:
        if action == "skip":
            flowrows.skip(conn, pay_id, user["name"], why)
            store.log(conn, user, "пропустил оплату", str(pay_id), why)
            return _rows_back(url, "Оплата убрана из входящих.")
        pid = flowrows.place(conn, pay_id, flow_id, user["name"])
    except flowrows.RowError as e:
        return _rows_back(url, err=str(e))
    store.log(conn, user, "разобрал оплату", str(pay_id), f"поток {flow_id}")
    return _rows_back(f"/rows?flow={flow_id}#r{pid}", "Оплата легла в строку.")


@app.get("/rows/{pid}", response_class=HTMLResponse)
def row_page(request: Request, pid: int, user: sqlite3.Row = Depends(current_user),
             conn: sqlite3.Connection = Depends(db)):
    require(access.manages_flows(user["role"]))
    r = _row_or_404(conn, pid)
    money = access.sees_finance(user["role"])
    info = person_info.facts(conn, r["card"], money)
    return page(request, "row.html", user, {
        **r, "cols": [c for c in flowrows.COLUMNS if money or not c[2]],
        "open_flows": flowrows.open_flows(conn), "money": money, "fmt_phone": flowrows.fmt_phone,
        "proposed": [x for v in info.values() for x in v["proposed"]],
        "flow_tags": flowrows.flow_tags(conn, r["flow"]) if r["flow"] else [],
        "today": __import__("datetime").date.today().isoformat(),
        "msg": request.query_params.get("msg"), "err": request.query_params.get("err")})
