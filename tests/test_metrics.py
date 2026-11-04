"""Метрики пилота: время ответа родителю и удержание учителей (app/services/metrics.py)."""
from datetime import date, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.bot.router import handle_update
from app.db import session
from app.demo_ids import DEMO_BASE
from app.models import QuestionTiming, SchoolMember, TeacherActivity, User
from app.services import faq, metrics, polls, retention, school, stats
from app.timeutil import utcnow
from app.webapp import api as webapi

from .conftest import CHAT_ID, PARENTS, TEACHER, cb_update, msg_update

T = TEACHER["user_id"]
P1, P2, P3 = (p["user_id"] for p in PARENTS)


def run(loop, coro):
    return loop.run_until_complete(coro)


def connect(loop):
    run(loop, handle_update({"update_type": "bot_added", "timestamp": 1, "chat_id": CHAT_ID, "user": TEACHER,
                             "is_channel": False}))

    async def work_hours():  # учитель «в рабочем времени»: переданный вопрос уходит сразу
        async with session() as s:
            (await s.get(User, T)).quiet_enabled = False
    run(loop, work_hours())


def timings(loop):
    async def go():
        async with session() as s:
            return (await s.scalars(select(QuestionTiming).order_by(QuestionTiming.id))).all()
    return run(loop, go())


def last_payload(api, prefix):
    for s in reversed(api.sent):
        for att in s.get("attachments") or []:
            for row in att.get("payload", {}).get("buttons", []):
                for b in row:
                    if (b.get("payload") or "").startswith(prefix):
                        return b["payload"]
    raise AssertionError(f"нет кнопки {prefix}")


# ====================================================================== расчёт на синтетике
def test_median_p90_and_shares_on_synthetic_rows():
    now = datetime(2026, 11, 2, 12, 0)
    secs = [2, 3, 3, 4, 5, 6, 8, 40, 300, 7200]  # 7 ответов бота за секунды, 3 — учителя
    rows = [(now, now + timedelta(seconds=s), "bot" if s < 60 else "teacher", s >= 60, False) for s in secs]
    rows += [(now, None, None, True, False), (now, None, None, False, False)]  # без ответа: переданный и нет
    a = metrics.summarize(rows, 30)
    assert a["asked"] == 12 and a["answered"] == 10 and a["unanswered"] == 2 and a["unanswered_forwarded"] == 1
    assert a["median_sec"] == 5.5            # (5 + 6) / 2
    assert a["p90_sec"] == 300               # 9-е из 10 по возрастанию
    assert a["bot_pct"] == 80 and a["by_teacher"] == 2
    assert a["under_minute_pct"] == 80
    assert a["teacher_median_sec"] == 3750   # (300 + 7200) / 2
    assert metrics.median([]) is None and metrics.percentile([], 90) is None
    assert metrics.fmt_duration(5.5) == "6 с" and metrics.fmt_duration(3750) == "1 ч 2 мин"
    lines = "\n".join(metrics.answer_lines(a))
    assert "Медиана: <b>6 с</b>" in lines and "Без ответа: 2" in lines


def test_cohort_retention_on_synthetic_activity():
    today = date(2026, 11, 30)
    w1 = date(2026, 9, 7)  # понедельник: когорта из 10 учителей
    rows = []
    for i in range(10):
        first = w1 + timedelta(days=i % 3)
        rows.append((i, first.isoformat(), False))
        if i < 8:   # 8 из 10 вернулись на второй неделе
            rows.append((i, (first + timedelta(days=9)).isoformat(), False))
        if i < 7:   # 7 из 10 — через месяц (окно 30–36 дней)
            rows.append((i, (first + timedelta(days=33)).isoformat(), False))
        if i == 9:  # активен только на 40-й день — в окно D30 не попал
            rows.append((i, (first + timedelta(days=40)).isoformat(), False))
    # маленькая когорта (3 человека) — проценты не показываем; окно D30 у неё ещё не прошло
    for i in range(100, 103):
        rows.append((i, "2026-10-26", False))
        rows.append((i, "2026-11-04", False))
    # свежая когорта: окно D30 ещё не прошло
    for i in range(200, 206):
        rows.append((i, "2026-11-23", False))
    r = metrics.cohorts(rows, today)
    by_week = {c["week"]: c for c in r["cohorts"]}
    old = by_week["2026-09-07"]
    assert old["size"] == 10 and old["d7"]["pct"] == 80 and old["d30"]["pct"] == 70
    small = by_week["2026-10-26"]
    assert small["size"] == 3 and small["d7"]["kept"] == 3 and small["d7"]["pct"] is None  # меньше MIN_COHORT
    fresh = by_week["2026-11-23"]
    assert fresh["d7"]["eligible"] == 0 and fresh["d30"]["eligible"] == 0
    # итог: D30 считается только по тем, у кого окно прошло (10 человек из первой когорты)
    assert r["d30"]["eligible"] == 10 and r["d30"]["pct"] == 70 and r["teachers"] == 19
    assert r["d7"]["eligible"] == 13 and r["d7"]["kept"] == 11
    text = "\n".join(metrics.retention_lines(r))
    assert "Вернулись через 30 дней: <b>70 %</b> — 7 из 10" in text
    assert r["demo"] is False and metrics.cohorts([(1, "2026-11-01", True)], today)["demo"] is True


# ====================================================================== замер вживую
def test_answer_time_is_recorded_for_bot_and_teacher(env):
    loop, api, c = env
    connect(loop)
    run(loop, faq.add_entry(T, "Во сколько собрание?", "В четверг в 18:30."))
    # 1) справочник ответил сразу
    run(loop, handle_update(msg_update(PARENTS[0], "Во сколько собрание?", mid="a1")))
    # 2) ответа нет — родитель передаёт вопрос, учитель отвечает
    run(loop, handle_update(msg_update(PARENTS[1], "Когда каникулы в этом году?", mid="a2")))
    run(loop, handle_update(cb_update(PARENTS[1], "fw:ask", "a3")))
    qid = last_payload(api, "fr:").split(":", 1)[1]
    rows = timings(loop)
    assert [(r.answered_by, r.forwarded) for r in rows] == [("bot", False), (None, True)]

    async def age_question():  # вопрос задан 3 минуты назад
        async with session() as s:
            row = await s.get(QuestionTiming, rows[1].id)
            row.asked_at = utcnow() - timedelta(minutes=3)
    run(loop, age_question())
    run(loop, handle_update(cb_update(TEACHER, f"fr:{qid}", "a4")))
    run(loop, handle_update(msg_update(TEACHER, "С 28 декабря по 11 января.", mid="a5")))
    rows = timings(loop)
    assert rows[1].answered_by == "teacher" and 170 <= (rows[1].answered_at - rows[1].asked_at).total_seconds() < 300
    assert all(r.teacher_id == T and r.chat_id == CHAT_ID and r.channel == "dm" for r in rows)

    # 3) упоминание бота в чате — ответ из справочника, канал chat
    run(loop, handle_update(msg_update(PARENTS[2], "@peremena_test_bot во сколько собрание?", chat_id=CHAT_ID,
                                       mid="a6")))
    assert timings(loop)[-1].channel == "chat" and timings(loop)[-1].answered_by == "bot"

    p = run(loop, stats.pilot(T))
    a = p["answer"]
    assert a["answered"] == 3 and a["by_bot"] == 2 and a["bot_pct"] == 67 and a["median_sec"] < 60
    assert p["answer_median_sec"] == a["median_sec"]
    run(loop, handle_update(msg_update(TEACHER, "/stats", mid="a7")))
    text = api.texts_to(user_id=T)[-1]
    assert "Время ответа родителям, 30 дней" in text and "Бот ответил сам: 2 из 3" in text


def test_not_that_reopens_and_suggestion_closes(env):
    loop, api, c = env
    connect(loop)
    e = run(loop, faq.add_entry(T, "Какая форма на физкультуру?", "Белая футболка, тёмные шорты."))
    run(loop, handle_update(msg_update(PARENTS[0], "Какая форма на физкультуру?", mid="b1")))
    assert timings(loop)[0].answered_by == "bot"
    run(loop, handle_update(cb_update(PARENTS[0], f"fq:no:{e.id}", "b2")))
    row = timings(loop)[0]
    assert row.answered_at is None  # «Это не то» — ответа не было
    run(loop, handle_update(cb_update(PARENTS[0], "fw:ask", "b3")))
    assert timings(loop)[0].forwarded is True and len(timings(loop)) == 1  # тот же вопрос, не новый


def test_activity_is_one_row_per_day_and_platform_skips_demo(env):
    loop, api, c = env
    connect(loop)
    run(loop, polls.create_poll(T, CHAT_ID, "consent", "Экскурсия", [], None))
    run(loop, faq.add_entry(T, "Где справка?", "В медкабинете."))
    run(loop, handle_update(msg_update(PARENTS[0], "/start", mid="c1")))  # родитель — не учитель

    async def rows():
        async with session() as s:
            return (await s.execute(select(TeacherActivity.user_id, TeacherActivity.day))).all()
    assert run(loop, rows()) == [(T, utcnow().date().isoformat())]

    async def demo_rows():
        async with session() as s:
            s.add(QuestionTiming(teacher_id=DEMO_BASE + 1, channel="dm", asked_at=utcnow(), answered_at=utcnow(),
                                 answered_by="bot", demo=True))
            s.add(QuestionTiming(teacher_id=T, channel="dm", asked_at=utcnow() - timedelta(seconds=4),
                                 answered_at=utcnow(), answered_by="bot"))
    run(loop, demo_rows())
    plat = run(loop, metrics.answer_time(("platform", None), 30))
    assert plat["asked"] == 1 and plat["demo"] is False  # песочница в метрики платформы не попадает


def test_admin_metrics_access(env, monkeypatch):
    loop, api, c = env
    connect(loop)
    teacher = run(loop, webapi.chats.get_user(T))
    with pytest.raises(HTTPException) as e:
        run(loop, webapi.admin_metrics("", teacher))
    assert e.value.status_code == 403

    monkeypatch.setattr(c.settings, "admin_max_ids", f"{T}, 777")
    rep = run(loop, webapi.admin_metrics("", teacher))
    assert rep["scope"] == "platform" and rep["scopes"] == ["platform"] and "retention" in rep
    monkeypatch.setattr(c.settings, "admin_max_ids", "")

    sch = run(loop, school.create_school(T, "Школа № 7"))  # создатель школы — её администратор
    rep = run(loop, webapi.admin_metrics("school", teacher))
    assert rep["scope"] == "school" and rep["title"] == "Школа № 7"
    with pytest.raises(HTTPException):
        run(loop, webapi.admin_metrics("platform", teacher))

    async def members():
        async with session() as s:
            return (await s.scalar(select(func.count()).select_from(SchoolMember)
                                   .where(SchoolMember.school_id == sch.id)))
    assert run(loop, members()) == 1
    run(loop, handle_update(msg_update(TEACHER, "/метрики", mid="d1")))
    assert "Метрики пилота · Школа № 7" in api.texts_to(user_id=T)[-1]


def test_retention_cleanup_drops_old_timings(env):
    loop, api, c = env

    async def seed():
        async with session() as s:
            s.add_all([
                QuestionTiming(teacher_id=1, channel="dm", asked_at=utcnow() - timedelta(days=181)),
                QuestionTiming(teacher_id=1, channel="dm", asked_at=utcnow() - timedelta(days=10)),
                TeacherActivity(user_id=1, day=(utcnow() - timedelta(days=401)).date().isoformat()),
                TeacherActivity(user_id=1, day=utcnow().date().isoformat()),
            ])
    run(loop, seed())
    out = run(loop, retention.cleanup())
    assert out["timings"] == 1 and out["activity"] == 1
    assert len(timings(loop)) == 1
