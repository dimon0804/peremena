"""Метрики пилота, которых нет в «Счётчике пользы»: время ответа родителю и удержание учителей.

**Время ответа.** На каждый вопрос родителя (в личке, в мини-приложении или упоминанием бота в чате)
пишется одна строка: когда спросили и когда пришёл первый ответ — от бота (справочник, доска, полка)
или от учителя (ответил на переданный вопрос или реплаем в чате). Текста вопроса и того, кто спросил,
в строке нет. Вопросы без ответа считаются отдельно и в медиану не попадают.
Если родитель нажал «Это не то», ответ бота не засчитывается: вопрос снова ждёт ответа.

**Удержание.** Учитель «активен в день», если сделал что-то осмысленное: создал или закрыл сбор, напомнил,
добавил карточку, ответил на вопрос, записал домашку, создал запись на встречу, открыл кабинет или команду
бота. Одна строка на учителя в день. Когорта — неделя первого действия. D7 — учитель был активен
хотя бы раз на 7–13-й день после первого действия, D30 — на 30–36-й день. Учитель попадает в расчёт,
только когда его окно целиком прошло; когорты меньше MIN_COHORT человек в процентах не показываются.

Строки демо-песочниц (особый диапазон id) в метрики платформы не попадают никогда, а строки, созданные
скриптом демо-данных, помечены demo=True — тогда ответ API и текст бота прямо говорят, что это демо, а не пилот.
"""
from __future__ import annotations

import html
import logging
import math
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from ..core import ctx
from ..db import session, utcnow
from ..demo_ids import DEMO_BASE, DEMO_SPAN, is_demo
from ..models import QuestionTiming, SchoolMember, TeacherActivity

log = logging.getLogger(__name__)

ANSWER_GOAL_SEC = 60      # «Медианное время ответа родителю < 1 минуты»
RETENTION_GOAL = 70       # «Удержание учителя через 30 дней ≥ 70 %»
MIN_COHORT = 5            # меньше — проценты по когорте не показываем: один человек даёт ±20 %
TIMING_DAYS = 180         # срок хранения замеров времени ответа (app/services/retention.py)
ACTIVITY_DAYS = 400       # срок хранения активных дней: хватает на учебный год и когорты прошлой осени
WINDOWS = {"d7": (7, 13), "d30": (30, 36)}

Scope = tuple[str, int | None]  # ("teacher", id) | ("school", id) | ("platform", None)


# ====================================================================== время ответа: запись
def asked_at_of(ts_ms) -> datetime:
    """Момент вопроса — время сообщения в MAX (мс), а не момент обработки: очередь событий тоже часть ожидания.
    Если времени нет или оно странное (часы сервера, тестовые события) — текущее время."""
    now = utcnow()
    try:
        at = datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError, OSError):
        return now
    return at if timedelta(0) <= now - at <= timedelta(hours=6) else now


async def bot_answered(teacher_id: int | None, chat_id: int | None, channel: str,
                       asked_at: datetime | None = None) -> int | None:
    """Бот ответил сразу: строка с вопросом и ответом. Возвращает id (для «Это не то»)."""
    return await _insert(teacher_id, chat_id, channel, asked_at, answered=True)


async def opened(teacher_id: int | None, chat_id: int | None, channel: str,
                 asked_at: datetime | None = None) -> int | None:
    """Вопрос без ответа: ответ придёт позже (учитель, выбор подсказки) или не придёт вовсе."""
    return await _insert(teacher_id, chat_id, channel, asked_at, answered=False)


async def _insert(teacher_id, chat_id, channel, asked_at, answered: bool) -> int | None:
    if not teacher_id:
        return None
    now = utcnow()
    try:
        async with session() as s:
            row = QuestionTiming(teacher_id=teacher_id, chat_id=chat_id, channel=channel[:10],
                                 asked_at=min(asked_at or now, now), demo=is_demo(teacher_id),
                                 answered_at=now if answered else None, answered_by="bot" if answered else None)
            s.add(row)
            await s.flush()
            return row.id
    except Exception:  # замер не должен ломать ответ родителю
        log.exception("Не записан замер времени ответа")
        return None


async def close(timing_id: int | None, by: str) -> bool:
    """Первый ответ на вопрос (bot | teacher). Повторный ответ время не меняет."""
    if not timing_id:
        return False
    try:
        async with session() as s:
            res = await s.execute(update(QuestionTiming).where(
                QuestionTiming.id == int(timing_id), QuestionTiming.answered_at.is_(None),
            ).values(answered_at=utcnow(), answered_by=by))
            return bool(res.rowcount)
    except Exception:
        log.exception("Не отмечен ответ на вопрос %s", timing_id)
        return False


async def reopen(timing_id: int | None) -> None:
    """«Это не то»: ответ бота не помог — вопрос снова ждёт ответа (теперь, скорее всего, учителя)."""
    if not timing_id:
        return
    try:
        async with session() as s:
            await s.execute(update(QuestionTiming).where(
                QuestionTiming.id == int(timing_id), QuestionTiming.answered_by == "bot",
            ).values(answered_at=None, answered_by=None))
    except Exception:
        log.exception("Не открыт заново вопрос %s", timing_id)


async def mark_forwarded(timing_id: int | None, teacher_id: int, chat_id: int | None) -> None:
    if not timing_id:
        return
    try:
        async with session() as s:
            await s.execute(update(QuestionTiming).where(QuestionTiming.id == int(timing_id))
                            .values(forwarded=True, teacher_id=teacher_id, chat_id=chat_id))
    except Exception:
        log.exception("Не отмечена передача вопроса %s", timing_id)


# ====================================================================== активные дни учителя
async def touch(user_id: int | None, staff_only: bool = False) -> None:
    """Учитель сделал что-то осмысленное сегодня. В базу — не чаще раза в день на человека (отметка в KV).
    staff_only — проверить, что это учитель (для команд бота и входа в кабинет: их шлют и родители)."""
    if not user_id:
        return
    day = utcnow().date().isoformat()
    try:
        if not await ctx().kv.add_once(f"act:{user_id}:{day}", ttl=2 * 24 * 3600):
            return
        if staff_only and not await _is_staff(user_id):
            return
        async with session() as s:
            if await s.scalar(select(TeacherActivity.id).where(
                    TeacherActivity.user_id == user_id, TeacherActivity.day == day)):
                return
            s.add(TeacherActivity(user_id=user_id, day=day, demo=is_demo(user_id)))
    except IntegrityError:
        pass  # тот же день уже записал другой процесс
    except Exception:
        log.exception("Не записан активный день учителя %s", user_id)


async def _is_staff(user_id: int) -> bool:
    from . import school

    return (await school.roles(user_id)).is_staff


# ====================================================================== расчёт
def _not_demo(col):
    return ~col.between(DEMO_BASE, DEMO_BASE + DEMO_SPAN)


def _who(col, scope: Scope):
    kind, sid = scope
    if kind == "teacher":
        return col == sid
    if kind == "school":
        return col.in_(select(SchoolMember.user_id).where(SchoolMember.school_id == sid))
    return _not_demo(col)  # платформа: демо-песочницы не считаем


def median(xs: list[float]) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    n, mid = len(xs), len(xs) // 2
    return float(xs[mid]) if n % 2 else (xs[mid - 1] + xs[mid]) / 2


def percentile(xs: list[float], p: float) -> float | None:
    """Ближайший ранг: p90 — время, быстрее которого получили ответ 90 % спросивших."""
    if not xs:
        return None
    xs = sorted(xs)
    return float(xs[max(0, math.ceil(p / 100 * len(xs)) - 1)])


async def answer_time(scope: Scope, days: int = 30) -> dict:
    since = utcnow() - timedelta(days=days)
    q = select(QuestionTiming.asked_at, QuestionTiming.answered_at, QuestionTiming.answered_by,
               QuestionTiming.forwarded, QuestionTiming.demo).where(
        QuestionTiming.asked_at >= since, _who(QuestionTiming.teacher_id, scope))
    async with session() as s:
        rows = (await s.execute(q)).all()
    return summarize(rows, days)


def summarize(rows, days: int = 30) -> dict:
    """rows — (asked_at, answered_at, answered_by, forwarded, demo)."""
    done = [(max(0.0, (ans - asked).total_seconds()), by) for asked, ans, by, _, _ in rows if ans is not None]
    secs = [d for d, _ in done]
    teacher = [d for d, by in done if by == "teacher"]
    pending = [fw for _, ans, _, fw, _ in rows if ans is None]
    by_bot = sum(1 for _, by in done if by == "bot")
    n = len(done)
    pct = (lambda k: round(100 * k / n)) if n else (lambda k: None)
    return {
        "days": days, "asked": len(rows), "answered": n,
        "unanswered": len(pending), "unanswered_forwarded": sum(1 for fw in pending if fw),
        "by_bot": by_bot, "by_teacher": n - by_bot, "bot_pct": pct(by_bot),
        "median_sec": median(secs), "p90_sec": percentile(secs, 90),
        "under_minute_pct": pct(sum(1 for d in secs if d < ANSWER_GOAL_SEC)),
        "teacher_median_sec": median(teacher),
        "goal_sec": ANSWER_GOAL_SEC, "demo": any(r[4] for r in rows),
    }


async def retention(scope: Scope, today: date | None = None) -> dict:
    q = select(TeacherActivity.user_id, TeacherActivity.day, TeacherActivity.demo).where(
        _who(TeacherActivity.user_id, scope))
    async with session() as s:
        rows = (await s.execute(q)).all()
    return cohorts(rows, today or utcnow().date())


def cohorts(rows, today: date) -> dict:
    """rows — (user_id, 'YYYY-MM-DD', demo). Когорты по неделе первого действия, новые сверху."""
    days: dict[int, set[date]] = {}
    demo = False
    for uid, day, is_demo_row in rows:
        days.setdefault(uid, set()).add(date.fromisoformat(day))
        demo = demo or bool(is_demo_row)
    groups: dict[date, list[set[date]]] = {}
    for active in days.values():
        first = min(active)
        groups.setdefault(first - timedelta(days=first.weekday()), []).append(active)

    def window(users: list[set[date]], key: str) -> dict:
        lo, hi = WINDOWS[key]
        eligible = kept = 0
        for active in users:
            first = min(active)
            if today <= first + timedelta(days=hi):
                continue  # окно ещё не прошло — рано судить
            eligible += 1
            kept += any(first + timedelta(days=lo) <= d <= first + timedelta(days=hi) for d in active)
        return {"eligible": eligible, "kept": kept,
                "pct": round(100 * kept / eligible) if eligible >= MIN_COHORT else None}

    out = []
    for week in sorted(groups, reverse=True):
        users = groups[week]
        out.append({"week": week.isoformat(), "size": len(users),
                    "d7": window(users, "d7"), "d30": window(users, "d30")})
    everyone = [a for users in groups.values() for a in users]
    return {"teachers": len(everyone), "cohorts": out[:12], "d7": window(everyone, "d7"),
            "d30": window(everyone, "d30"), "min_cohort": MIN_COHORT, "goal": RETENTION_GOAL, "demo": demo}


async def scopes_for(user_id: int) -> list[tuple[Scope, str]]:
    """Какие метрики человек вправе видеть: платформа (ADMIN_MAX_IDS) и/или своя школа (завуч)."""
    from . import school

    out: list[tuple[Scope, str]] = []
    if user_id in ctx().settings.admin_ids:
        out.append((("platform", None), "Вся платформа"))
    r = await school.roles(user_id)
    if r.is_admin and r.school:
        out.append((("school", r.school.id), r.school.name))
    return out


async def report(scope: Scope) -> dict:
    """Всё для экрана метрик: время ответа за 7 и 30 дней и удержание."""
    return {"scope": scope[0], "answer_7": await answer_time(scope, 7), "answer_30": await answer_time(scope, 30),
            "retention": await retention(scope)}


# ====================================================================== текст для людей
def fmt_duration(sec: float | None) -> str:
    if sec is None:
        return "—"
    sec = int(round(sec))
    if sec < 60:
        return f"{sec} с"
    if sec < 3600:
        m, s = divmod(sec, 60)
        return f"{m} мин {s} с" if s and m < 10 else f"{m} мин"
    if sec < 86400:
        h, m = divmod(sec // 60, 60)
        return f"{h} ч {m} мин" if m else f"{h} ч"
    d, h = divmod(sec // 3600, 24)
    return f"{d} дн {h} ч" if h else f"{d} дн"


def answer_lines(a: dict) -> list[str]:
    """Строки «Время ответа родителям» для /stats и /metrics."""
    if not a["asked"]:
        return [f"Вопросов от родителей за {a['days']} дней пока не было."]
    lines = []
    if a["answered"]:
        med = a["median_sec"]
        mark = "✅" if med is not None and med < a["goal_sec"] else "⏳"
        lines.append(f"{mark} Медиана: <b>{fmt_duration(med)}</b> (цель — меньше минуты), "
                     f"9 из 10 ответов — не дольше {fmt_duration(a['p90_sec'])}")
        lines.append(f"Быстрее минуты: {a['under_minute_pct']} %. Бот ответил сам: {a['by_bot']} из {a['answered']}"
                     + (f", учитель — {a['by_teacher']} (медиана {fmt_duration(a['teacher_median_sec'])})"
                        if a["by_teacher"] else ""))
    if a["unanswered"]:
        wait = f", из них переданы учителю: {a['unanswered_forwarded']}" if a["unanswered_forwarded"] else ""
        lines.append(f"Без ответа: {a['unanswered']}{wait} — в медиану не входят.")
    return lines


def retention_lines(r: dict) -> list[str]:
    if not r["teachers"]:
        return ["Активных учителей пока нет — удержание появится, когда учителя начнут работать с ботом."]
    lines = [f"Учителей в расчёте: {r['teachers']}."]
    for key, label in (("d7", "через неделю"), ("d30", "через 30 дней")):
        w = r[key]
        if w["pct"] is not None:
            goal = f" (цель — не меньше {r['goal']} %)" if key == "d30" else ""
            lines.append(f"• Вернулись {label}: <b>{w['pct']} %</b> — {w['kept']} из {w['eligible']}{goal}")
        elif w["eligible"]:
            lines.append(f"• Вернулись {label}: {w['kept']} из {w['eligible']} — "
                         f"мало данных для процента (нужно от {r['min_cohort']})")
        else:
            lines.append(f"• Вернулись {label}: рано считать — окно ещё не прошло ни у кого.")
    shown = [c for c in r["cohorts"] if c["size"] >= r["min_cohort"]][:6]
    if shown:
        lines.append("По неделе, когда учитель начал (через неделю / через 30 дней):")

        def cell(w: dict) -> str:
            if w["pct"] is not None:
                return f"{w['pct']} %"
            return "мало данных" if w["eligible"] else "рано"
        for c in shown:
            d = date.fromisoformat(c["week"])
            lines.append(f"   с {d:%d.%m}, {c['size']} чел.: {cell(c['d7'])} / {cell(c['d30'])}")
    return lines


def render(rep: dict, title: str) -> str:
    demo = rep["answer_30"]["demo"] or rep["retention"]["demo"]
    lines = [f"📈 <b>Метрики пилота · {html.escape(title)}</b>", ""]
    if demo:
        lines += ["⚠️ <i>Посчитано на демо-данных. Это проверка расчёта, а не результат пилота.</i>", ""]
    for key, label in (("answer_7", "7 дней"), ("answer_30", "30 дней")):
        lines.append(f"<b>Время ответа родителю, {label}</b>")
        lines += answer_lines(rep[key])
        lines.append("")
    lines.append("<b>Удержание учителей</b>")
    lines += retention_lines(rep["retention"])
    return "\n".join(lines).strip()
