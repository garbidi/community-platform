"""Экраны мастера настройки.

Логика — в setup.py, здесь только маршруты и то, что видит хозяин.
Каждый шаг: показать форму, принять, тут же проверить, сказать по-русски,
что не так. Ошибка должна всплывать на том же экране, где её можно
исправить.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import setup, store

router = APIRouter()
COOKIE = "vershina_setup"


def bind(templates: Jinja2Templates) -> APIRouter:
    """Роутер берёт шаблоны приложения, чтобы вид был единым."""
    router.templates = templates          # type: ignore[attr-defined]
    return router


def _t(request: Request, step: str, **extra) -> HTMLResponse:
    data = {
        "step": step,
        "steps": setup.STEPS,
        "state": setup.state(),
        "title": dict(setup.STEPS).get(step, "Настройка"),
    }
    data.update(extra)
    return router.templates.TemplateResponse(  # type: ignore[attr-defined]
        request, "setup.html", data)


def _allowed(request: Request) -> bool:
    """Мастер открыт либо по одноразовой ссылке, либо по выданной ей метке."""
    if setup.is_done():
        return False
    if setup.key_ok(request.query_params.get("key")):
        return True
    return setup.key_ok(request.cookies.get(COOKIE))


def _closed(request: Request) -> HTMLResponse:
    return router.templates.TemplateResponse(  # type: ignore[attr-defined]
        request, "setup_closed.html", {"done": setup.is_done()}, status_code=403)


# ------------------------------------------------------------------ вход

def _skip_ready(step: str) -> str:
    """Проматывает шаги, которые уже сделаны в обход мастера.

    Вход в Claude обычно делает не хозяин, а тот, кто ставил помощника:
    шаг требует терминала и подписки. Если вход уже есть, показывать
    хозяину кнопку «Войти» незачем — она только собьёт с толку.
    """
    guard = 0
    while guard < len(setup.STEP_KEYS):
        if step == "claude" and setup.claude_ready():
            step = setup.advance("claude")
        elif step == "telegram" and setup.tg_ready():
            step = setup.advance("telegram")
        else:
            return step
        guard += 1
    return step


@router.get("/setup", response_class=HTMLResponse)
def show(request: Request):
    if not _allowed(request):
        return _closed(request)
    page = _t(request, _skip_ready(setup.current_step()))
    key = request.query_params.get("key")
    if key:
        # Дальше ключ носит метка: хозяину не придётся держать ссылку
        # открытой и переходить по ней на каждом шаге.
        page.set_cookie(COOKIE, key, httponly=True, samesite="lax",
                        max_age=60 * 60 * 6)
    return page


# --------------------------------------------------------------- учётка

@router.post("/setup/account", response_class=HTMLResponse)
def account(request: Request, name: str = Form(...), login: str = Form(...),
            password: str = Form(...), password2: str = Form("")):
    if not _allowed(request):
        return _closed(request)
    if password != password2:
        return _t(request, "account", error="Пароли не совпали.",
                  values={"name": name, "login": login})
    if len(password) < 8:
        return _t(request, "account",
                  error="Пароль короче восьми знаков — его легко подобрать.",
                  values={"name": name, "login": login})
    conn = store.connect()
    try:
        if store.user_by_login(conn, login):
            return _t(request, "account",
                      error="Такой логин уже занят, придумайте другой.",
                      values={"name": name, "login": login})
        store.create_user(conn, login, name, "admin", password)
        conn.commit()
    finally:
        conn.close()
    setup.advance("account")
    return RedirectResponse("/setup", status_code=303)


# ------------------------------------------------------------------- бот

@router.post("/setup/bot", response_class=HTMLResponse)
def bot(request: Request, token: str = Form(...)):
    if not _allowed(request):
        return _closed(request)
    res = setup.check_bot_token(token)
    if not res["ok"]:
        return _t(request, "bot", error=res["error"])
    setup.write_env(setup.BOT_ENV, {"VERSHINA_BOT_TOKEN": token.strip()})
    setup.put("bot_username", res["username"] or "")
    setup.put("owner_since", str(int(time.time())))
    setup.advance("bot")
    return RedirectResponse("/setup", status_code=303)


# ------------------------------------------------------------- кто хозяин

@router.post("/setup/owner", response_class=HTMLResponse)
def owner(request: Request):
    """Ловит того, кто написал боту. Телеграм-id вводить не нужно."""
    if not _allowed(request):
        return _closed(request)
    env = setup.read_env(setup.BOT_ENV)
    token = env.get("VERSHINA_BOT_TOKEN", "")
    since = float(setup.get("owner_since", "0") or 0)
    found = setup.catch_owner(token, since)
    if not found:
        return _t(request, "owner", error=(
            "Пока не вижу от вас сообщения. Напишите боту любое слово "
            "и нажмите «Проверить» ещё раз."))
    setup.write_env(setup.BOT_ENV, {"VERSHINA_BOT_ALLOW": str(found["id"])})
    setup.put("owner_name", found["name"])
    setup.advance("owner")
    return RedirectResponse("/setup", status_code=303)


# -------------------------------------------------------------- телеграм

@router.post("/setup/telegram/phone", response_class=HTMLResponse)
def tg_phone(request: Request, phone: str = Form(...)):
    if not _allowed(request):
        return _closed(request)
    res = setup.tg_send_code(phone.strip())
    if not res.get("ok"):
        return _t(request, "telegram", error=res.get("error"))
    if res.get("already"):
        setup.advance("telegram")
        return RedirectResponse("/setup", status_code=303)
    return _t(request, "telegram", sent=True)


@router.post("/setup/telegram/code", response_class=HTMLResponse)
def tg_code(request: Request, code: str = Form(...), password: str = Form("")):
    if not _allowed(request):
        return _closed(request)
    res = setup.tg_sign_in(code, password)
    if res.get("need_password"):
        return _t(request, "telegram", sent=True, need_password=True)
    if not res.get("ok"):
        return _t(request, "telegram", sent=True, error=res.get("error"),
                  need_password=bool(password))
    setup.advance("telegram")
    return RedirectResponse("/setup", status_code=303)


# ---------------------------------------------------------------- claude

@router.post("/setup/claude/start", response_class=HTMLResponse)
def claude_start(request: Request):
    if not _allowed(request):
        return _closed(request)
    res = setup.claude_login_start()
    if not res.get("ok"):
        return _t(request, "claude", error=res.get("error"), fallback=True)
    return _t(request, "claude", login_url=res["url"])


@router.post("/setup/claude/code", response_class=HTMLResponse)
def claude_code(request: Request, code: str = Form(...)):
    if not _allowed(request):
        return _closed(request)
    res = setup.claude_login_finish(code)
    if not res.get("ok"):
        return _t(request, "claude", error=res.get("error"), fallback=True)
    setup.advance("claude")
    return RedirectResponse("/setup", status_code=303)


@router.post("/setup/claude/token", response_class=HTMLResponse)
def claude_token(request: Request, value: str = Form(...)):
    """Запасной путь, если вход через ссылку не задался."""
    if not _allowed(request):
        return _closed(request)
    res = setup.claude_save_token(value)
    if not res.get("ok"):
        return _t(request, "claude", error=res.get("error"), fallback=True)
    setup.advance("claude")
    return RedirectResponse("/setup", status_code=303)


# ---------------------------------------------------------- необязательное

@router.post("/setup/extras", response_class=HTMLResponse)
def extras(request: Request):
    if not _allowed(request):
        return _closed(request)
    setup.advance("extras")
    return RedirectResponse("/setup", status_code=303)


# -------------------------------------------------------------- проверка

@router.post("/setup/check", response_class=HTMLResponse)
def check(request: Request):
    if not _allowed(request):
        return _closed(request)
    env = setup.read_env(setup.BOT_ENV)
    token = env.get("VERSHINA_BOT_TOKEN", "")
    allow = env.get("VERSHINA_BOT_ALLOW", "").split(",")[0]
    if not token or not allow:
        return _t(request, "check", error="Бот ещё не настроен.")
    sent = setup.send_message(
        token, int(allow),
        "Настройка закончена — я на связи. Напишите мне что-нибудь.")
    if not sent:
        return _t(request, "check", error=(
            "Не смог отправить вам сообщение в телеграм. Проверьте, что "
            "вы писали именно этому боту."))
    setup.finish()
    return _t(request, "check", sent=True)
