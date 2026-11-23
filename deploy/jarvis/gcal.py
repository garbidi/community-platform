"""Google Календарь хозяина: смотреть, создавать, напоминать в телеграм.

Подготовка (один раз):
  1. В gcal_client.json положить client_id и client_secret OAuth-клиента
     типа «Desktop app» (Google Cloud → APIs & Services → Credentials),
     у проекта должен быть включён Google Calendar API.
  2. python gcal.py auth            — напечатает ссылку для входа
  3. python gcal.py auth <адрес или код после входа>  — сохранит доступ

Дальше:
  python gcal.py check                       — какие календари доступны
  python gcal.py list [дней]                 — ближайшие события
  python gcal.py today                       — события на сегодня
  python gcal.py create "Тема" 2026-09-15 14:00 [минут] ["описание"]
  python gcal.py remind [минут]              — напомнить в телеграм о том,
                                               что начинается в ближайшие N минут
  python gcal.py digest                      — утренняя сводка дня в телеграм
Время указывается по Москве.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).parent
CLIENT = HERE / "gcal_client.json"
TOKEN = HERE / "gcal_token.json"
REMINDED = HERE / "gcal_reminded.json"
BOT_ENV = Path(os.environ.get(
    "VERSHINA_BOT_ENV",
    "/data/app/bot.env"))

TZ = ZoneInfo("Europe/Moscow")
SCOPES = ["https://www.googleapis.com/auth/calendar"]
REDIRECT = "http://localhost:8765/"
CALENDAR = "primary"


# ------------------------------------------------------------------ доступ

def _client_config():
    if not CLIENT.exists():
        raise SystemExit("Нет gcal_client.json с client_id и client_secret.")
    c = json.loads(CLIENT.read_text())
    if "installed" in c:
        return c
    return {"installed": {
        "client_id": c["client_id"],
        "client_secret": c["client_secret"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [REDIRECT],
    }}


def _flow():
    from google_auth_oauthlib.flow import Flow
    # Без PKCE: ссылка и обмен кода идут разными запусками, verifier
    # между ними не сохранить.
    return Flow.from_client_config(_client_config(), scopes=SCOPES,
                                   redirect_uri=REDIRECT,
                                   autogenerate_code_verifier=False)


def auth(answer=None):
    flow = _flow()
    if answer is None:
        url, _ = flow.authorization_url(access_type="offline", prompt="consent")
        return ("Откройте ссылку, войдите в Google и разрешите доступ к "
                "календарю. После этого браузер перейдёт на адрес, который "
                "не откроется — скопируйте его целиком из адресной строки "
                "и пришлите.\n\n" + url)
    if answer.startswith("http"):
        code = urllib.parse.parse_qs(urllib.parse.urlparse(answer).query)["code"][0]
    else:
        code = answer
    flow.fetch_token(code=code)
    TOKEN.write_text(flow.credentials.to_json())
    return "Доступ к календарю сохранён.\n" + check()


def _service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    if not TOKEN.exists():
        raise SystemExit("Доступ к календарю ещё не открыт: сначала auth.")
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN.write_text(creds.to_json())
        else:
            raise SystemExit("Доступ протух, нужно заново пройти auth.")
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


# ------------------------------------------------------------------ чтение

def _when(ev, key="start"):
    v = ev[key]
    if "dateTime" in v:
        return datetime.fromisoformat(v["dateTime"]).astimezone(TZ)
    return datetime.fromisoformat(v["date"]).replace(tzinfo=TZ)


def _fmt(ev):
    start = _when(ev)
    all_day = "date" in ev["start"]
    when = start.strftime("%d.%m %a") if all_day else start.strftime("%d.%m %a %H:%M")
    line = f"{when} — {ev.get('summary', '(без названия)')}"
    if ev.get("location"):
        line += f" @ {ev['location']}"
    if ev.get("_cal"):
        line += f" [{ev['_cal']}]"
    return line


def _calendars(svc):
    """Все календари, которые видны в аккаунте, кроме праздников."""
    items = svc.calendarList().list().execute().get("items", [])
    return [c for c in items if "holiday" not in c["id"]]


def _events(t_from, t_to, max_results=50):
    svc = _service()
    out = []
    for cal in _calendars(svc):
        res = svc.events().list(
            calendarId=cal["id"], timeMin=t_from.isoformat(),
            timeMax=t_to.isoformat(), singleEvents=True, orderBy="startTime",
            maxResults=max_results,
        ).execute()
        for e in res.get("items", []):
            if not cal.get("primary"):
                e["_cal"] = cal["summary"]
            out.append(e)
    out.sort(key=_when)
    return out


def check():
    svc = _service()
    cals = svc.calendarList().list().execute().get("items", [])
    lines = ["Календари:"]
    for c in cals:
        mark = " (основной)" if c.get("primary") else ""
        lines.append(f"- {c['summary']}{mark}, роль: {c.get('accessRole')}")
    lines.append("")
    lines.append(list_upcoming(7))
    return "\n".join(lines)


def list_upcoming(days=7):
    now = datetime.now(TZ)
    evs = _events(now, now + timedelta(days=int(days)))
    if not evs:
        return f"На ближайшие {days} дн. событий нет."
    return "\n".join(_fmt(e) for e in evs)


def today():
    now = datetime.now(TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    evs = _events(start, start + timedelta(days=1))
    if not evs:
        return "На сегодня событий нет."
    return "Сегодня:\n" + "\n".join(_fmt(e) for e in evs)


# ------------------------------------------------------------------ создание

def create(title, date, time, minutes=60, description=""):
    start = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    end = start + timedelta(minutes=int(minutes))
    body = {
        "summary": title,
        "description": description,
        "start": {"dateTime": start.isoformat(), "timeZone": "Europe/Moscow"},
        "end": {"dateTime": end.isoformat(), "timeZone": "Europe/Moscow"},
        "reminders": {"useDefault": False,
                      "overrides": [{"method": "popup", "minutes": 30}]},
    }
    ev = _service().events().insert(calendarId=CALENDAR, body=body).execute()
    return (f"Добавлено: {ev['summary']}\n"
            f"{start.strftime('%d.%m.%Y %H:%M')} МСК, {minutes} мин\n"
            f"{ev.get('htmlLink', '')}")


# ------------------------------------------------------------------ телеграм

def _bot():
    env = {}
    for line in BOT_ENV.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    token = env.get("VERSHINA_BOT_TOKEN")
    chats = [int(x) for x in env.get("VERSHINA_BOT_ALLOW", "").replace(" ", "").split(",") if x]
    if not token or not chats:
        raise SystemExit("Нет токена бота или адресата для напоминаний.")
    return token, chats


def send(text):
    token, chats = _bot()
    for chat in chats:
        data = json.dumps({"chat_id": chat, "text": text}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data,
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req).read()
    return f"Отправлено в телеграм ({len(chats)})."


def remind(minutes=60):
    now = datetime.now(TZ)
    evs = _events(now, now + timedelta(minutes=int(minutes)))
    done = json.loads(REMINDED.read_text()) if REMINDED.exists() else {}
    # выкидываем старые пометки, чтобы файл не рос
    cutoff = (now - timedelta(days=2)).isoformat()
    done = {k: v for k, v in done.items() if v > cutoff}
    fresh = [e for e in evs if e["id"] not in done and "dateTime" in e["start"]]
    if not fresh:
        REMINDED.write_text(json.dumps(done))
        return "Напоминать не о чем."
    for e in fresh:
        start = _when(e)
        left = int((start - now).total_seconds() // 60)
        text = f"Напоминаю: через {left} мин — {e.get('summary', '(без названия)')}, в {start.strftime('%H:%M')}."
        if e.get("location"):
            text += f"\nГде: {e['location']}"
        if e.get("description"):
            text += f"\n{e['description'][:300]}"
        send(text)
        done[e["id"]] = now.isoformat()
    REMINDED.write_text(json.dumps(done))
    return f"Напомнил о {len(fresh)}."


def digest():
    text = today()
    if text.startswith("На сегодня"):
        text = "Доброе утро! " + text
    else:
        text = "Доброе утро! " + text
    send(text)
    return text


if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args[0] if args else "check"
    if cmd == "auth":
        print(auth(args[1] if len(args) > 1 else None))
    elif cmd == "check":
        print(check())
    elif cmd == "list":
        print(list_upcoming(*args[1:2]))
    elif cmd == "today":
        print(today())
    elif cmd == "create":
        print(create(*args[1:]))
    elif cmd == "remind":
        print(remind(*args[1:2]))
    elif cmd == "digest":
        print(digest())
    elif cmd == "send":
        print(send(" ".join(args[1:])))
    else:
        print(__doc__)
