"""Тестовый поток «ММ8-тест» — полигон для механик без Google-таблиц.

Люди вымышленные, менторов нет — реальным менторам поток не виден. Единственный
чат — группа «Бот» (Станислав, Марья, аккаунт сообщества): на ней проверяем
отправку постов, замок в режиме «только тестовый чат» пускает только туда. Заполняется теми же функциями, что и кнопки
на платформе (app/decl.py), — заодно проверка механик.

    python -m app.testflow            создать и заполнить (если потока нет)
    python -m app.testflow --reset    удалить и создать заново
"""

from __future__ import annotations

import random
import sys
from datetime import date, timedelta

from . import decl, flows, store

NAME = "ММ8-тест"
START, FINISH = date(2026, 9, 21), date(2026, 11, 15)   # как у ММ7 — удобно сравнивать
NOTE = ("Тестовый поток: вымышленные участники, без менторов. Полигон для переезда с Google-таблиц. "
        "Чат — только «Бот», для проверки отправки постов.")
BOT_CHAT = {"chat_id": -5000000003, "topic_id": 0, "title": "Бот", "kind": "организаторы"}

TEAMS = {
    "Команда А — тест": ["Абрамов Пётр", "Белова Анна", "Волков Сергей", "Громова Ольга", "Дёмин Артур"],
    "Команда Б — тест": ["Егорова Мария", "Жуков Денис", "Зайцева Ирина", "Ильин Тимур"],
    "Команда В — тест": ["Карпова Елена", "Лебедев Олег", "Морозова Юлия", "Носов Кирилл", "Орлова Дина"],
}

GOALS = [
    ("Заработал {m} ₽ чистой прибыли", ["посчитал ФЭМ на цель", "запустил новый канал продаж",
                                        "вышел на {h} ₽ за месяц", "закрыл месяц с отчётом"], "выписка по счёту"),
    ("Нанял руководителя отдела продаж", ["описал профиль кандидата", "разместил вакансию",
                                          "провёл 5 собеседований", "вывел на испытательный срок"], "приказ о приёме"),
    ("Съездил на 5 экскурсий к предпринимателям", ["экскурсия 1", "экскурсия 2", "экскурсия 3",
                                                   "экскурсия 4", "экскурсия 5"], "фото и эссе"),
    ("Пробежал полумарафон", ["купил план тренировок", "пробежал 10 км", "пробежал 15 км",
                              "зарегистрировался на забег", "финишировал"], "фото с финиша"),
    ("Выстроил еженедельный отчёт команды", ["выбрал метрики", "собрал шаблон", "провёл 4 планёрки"], "таблица отчёта"),
    ("Провёл отпуск с семьёй без телефона", [], "фото"),
    ("Запустил новый продукт", ["провёл 10 кастдевов", "собрал прототип", "получил первые 3 оплаты"],
     "скрин оплат"),
]


def _drop(conn) -> None:
    f = flows.get(conn, name=NAME)
    if not f:
        return
    pids = [r["id"] for r in conn.execute("SELECT id FROM people WHERE flow=?", (NAME,))]
    for pid in pids:
        conn.execute("DELETE FROM decl_steps WHERE person_id=?", (pid,))
        conn.execute("DELETE FROM decl_goals WHERE person_id=?", (pid,))
        conn.execute("DELETE FROM involved WHERE person_id=?", (pid,))
        conn.execute("DELETE FROM money_weeks WHERE person_id=?", (pid,))
        conn.execute("DELETE FROM team_members WHERE tg_user_id=?", (-pid,))
        conn.execute("DELETE FROM assignments WHERE tg_user_id=?", (-pid,))
    conn.execute("DELETE FROM people WHERE flow=?", (NAME,))
    conn.execute("DELETE FROM team_engagement WHERE flow=?", (NAME,))
    flows.delete(conn, f["id"])


def seed(conn, today: date | None = None) -> str:
    today = today or date.today()
    if flows.get(conn, name=NAME):
        return f"{NAME} уже есть — для пересоздания: --reset"
    rnd = random.Random(8)
    fid = flows.create(conn, NAME, START.isoformat(), FINISH.isoformat())
    flows.update(conn, fid, NAME, START.isoformat(), FINISH.isoformat(), NOTE)
    elapsed = max(0.0, min(1.0, (today - START).days / (FINISH - START).days))
    n_people = 0
    for no, (title, people) in enumerate(TEAMS.items(), 1):
        tid = flows.add_team(conn, fid, title, "", [], [], no)
        for name in people:
            planka = rnd.choice([2, 3, 3, 4])
            pid = decl.add_person(conn, tid, name, "", planka)
            n_people += 1
            zeal = rnd.uniform(0.35, 1.0)           # насколько человек «в деле»
            for g_title, steps, proof in rnd.sample(GOALS, rnd.randint(4, 6)):
                m = rnd.choice([1, 2, 3, 5])
                g_dead = FINISH - timedelta(days=rnd.choice([0, 0, 7, 14, 21]))
                gid = decl.add_goal(conn, pid, g_title.format(m=f"{m} 000 000"), g_dead.isoformat(), proof)
                span = (g_dead - START).days
                for i, st in enumerate(steps, 1):
                    dl = START + timedelta(days=int(span * i / (len(steps) + 0.5)))
                    sid = decl.add_step(conn, gid, st.format(h=f"{m * 300} 000"), dl.isoformat())
                    # срок прошёл — скорее сделано; у «горящих» чаще и досрочно
                    due = dl <= today
                    if (due and rnd.random() < 0.55 + 0.45 * zeal) or (not due and rnd.random() < 0.15 * zeal):
                        decl.toggle_step(conn, sid, "тест")
                if not steps and rnd.random() < elapsed:
                    decl.close_goal(conn, gid, True)
            # вовлечения — по дням потока, у активных больше
            n_inv = int(rnd.random() * zeal * (planka + 2) * elapsed * 1.6 + 0.4)
            for d in sorted(START + timedelta(days=rnd.randint(0, max(0, (today - START).days)))
                            for _ in range(n_inv)):
                decl.add_involvement(conn, pid, d.isoformat(),
                                     rnd.choice(["большая программа"] * 4 + ["ступени", "интенсив"]))
            decl.set_terms(conn, pid, rnd.choice(["100 000 ₽", "200 000 ₽", "300 000 ₽"]),
                           rnd.choice(["часы от 1 млн", "поездка на Байкал", "новый ноутбук", ""]),
                           str(rnd.choice([1, 2, 3, 5]) * 1_000_000))
            for w in decl.weeks(flows.get(conn, fid)):
                if w["to"] <= today and rnd.random() < zeal:
                    decl.set_money(conn, pid, w["no"], str(rnd.randint(1, 40) * 25_000), "тест")
    conn.execute("INSERT OR IGNORE INTO flow_chats (flow_id, chat_id, title, kind, topic_id) VALUES (?,?,?,?,0)",
                 (fid, BOT_CHAT["chat_id"], BOT_CHAT["title"], BOT_CHAT["kind"]))
    # один выбывший — его планка делится между оставшимися в команде
    gone = conn.execute("SELECT id FROM people WHERE flow=? AND name=?", (NAME, "Ильин Тимур")).fetchone()
    decl.set_left(conn, gone["id"], True)
    return f"{NAME}: {len(TEAMS)} команды, {n_people} участников"


def main() -> None:
    conn = store.connect()
    decl.init(conn)
    if "--reset" in sys.argv:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _drop(conn)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    conn.execute("BEGIN IMMEDIATE")
    try:
        msg = seed(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    print(msg)


if __name__ == "__main__":
    main()
