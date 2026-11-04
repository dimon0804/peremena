"""Бумага, без которой школа пока не живёт: ведомость для подписи и бланки согласия с QR-кодом.

* Ведомость — итог сбора одной таблицей: родитель, ребёнок, ответ, когда, графа «Подпись». Её распечатывают
  для папки классного руководителя или для поездки (согласия на выезд всё ещё требуют подписи).
* Бланки — отрывные согласия по два на лист, с уже вписанными именами. На каждом QR-код: родитель наводит
  камеру и отвечает в MAX — ответ сразу попадает в сбор, учителю не нужно разбирать бумажки.

Страницы открываются по подписанной ссылке на сутки (как выгрузка CSV) — без входа, с телефона или компьютера.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import time

from sqlalchemy import select

from ..core import ctx
from ..db import session
from ..models import Member, Poll, School, SchoolMember
from ..services import chats, polls
from ..timeutil import fmt_dt, to_local

TTL = 24 * 3600
KINDS = ("sheet", "blanks")


def _sig(kind: str, poll_id: int, teacher_id: int, exp: int) -> str:
    msg = f"print:{kind}:{poll_id}:{teacher_id}:{exp}".encode()
    return hmac.new(ctx().settings.signing_key, msg, hashlib.sha256).hexdigest()[:32]


def url(p: Poll, kind: str, only_missing: bool = False) -> str:
    exp = int(time.time()) + TTL
    tail = "&only=missing" if only_missing else ""
    return f"/api/polls/{p.id}/print/{kind}?exp={exp}&sig={_sig(kind, p.id, p.teacher_id, exp)}{tail}"


def check(p: Poll | None, kind: str, exp: int, sig: str) -> bool:
    if p is None or kind not in KINDS or exp < time.time():
        return False
    return hmac.compare_digest(sig.encode(), _sig(kind, p.id, p.teacher_id, exp).encode())


e = html.escape

STYLE = """
:root{color-scheme:light}
*{box-sizing:border-box}
body{font:14px/1.45 -apple-system,"Segoe UI",Roboto,Arial,sans-serif;color:#111;background:#f3f4f6;margin:0}
.page{background:#fff;max-width:820px;margin:16px auto;padding:28px 32px;box-shadow:0 1px 4px rgba(0,0,0,.12)}
.bar{max-width:820px;margin:16px auto 0;display:flex;gap:10px;align-items:center;justify-content:space-between;padding:0 16px}
.bar button{font:inherit;padding:10px 18px;border-radius:10px;border:0;background:#2f6fed;color:#fff;cursor:pointer}
.bar span{color:#555;font-size:13px}
h1{font-size:19px;margin:0 0 4px}
.meta{color:#444;margin:0 0 14px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{border:1px solid #999;padding:6px 7px;text-align:left;vertical-align:top}
th{background:#eef0f3}
td.sig{width:22%}
td.n{width:30px;text-align:right}
.tot{margin-top:12px}
.foot{margin-top:28px;display:flex;justify-content:space-between;gap:24px}
.blank{border:1px dashed #888;padding:18px 20px;margin:0 0 18px;display:flex;gap:18px;page-break-inside:avoid;break-inside:avoid}
.blank .txt{flex:1}
.blank h2{font-size:15px;margin:0 0 6px}
.blank .body{white-space:pre-line;margin:0 0 10px}
.line{border-bottom:1px solid #333;display:inline-block;min-width:200px;padding:0 4px}
.opts{margin:10px 0}
.opts span{margin-right:18px;white-space:nowrap}
.qr{width:132px;text-align:center;font-size:11px;color:#333}
.qr svg{width:120px;height:120px}
.online{color:#0a7a3a;font-size:12px;margin-top:6px}
@media print{body{background:#fff}.bar{display:none}.page{box-shadow:none;margin:0;max-width:none;padding:0}
 .blank:nth-of-type(2n){page-break-after:always;break-after:page}}
@page{size:A4;margin:14mm}
"""


def _doc(title: str, body: str) -> str:
    return (f"<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<meta name=\"robots\" content=\"noindex\"><title>{e(title)}</title><style>{STYLE}</style></head>"
            f"<body><div class=\"bar\"><span>Страница готова к печати (A4)</span>"
            f"<button type=\"button\" data-print>Печать</button></div>{body}"
            f"<script src=\"/app/print.js\"></script></body></html>")


async def _context(p: Poll) -> tuple[str, str, str, dict[int, str]]:
    """(школа, класс, учитель, {user_id: имя ребёнка})."""
    chat = await chats.get_chat(p.chat_id)
    teacher = await chats.get_user(p.teacher_id)
    async with session() as s:
        sid = await s.scalar(select(SchoolMember.school_id).where(SchoolMember.user_id == p.teacher_id))
        sch = await s.get(School, sid) if sid else None
        kids = {m.user_id: m.child_name for m in (await s.scalars(
            select(Member).where(Member.chat_id == p.chat_id))).all() if m.child_name}
    return (sch.name if sch else "", chat.title if chat else "", teacher.display_name if teacher else "", kids)


async def sheet(p: Poll) -> str:
    school_name, cls, teacher, kids = await _context(p)
    owner = await chats.get_user(p.teacher_id)
    tz = owner.tz if owner else None
    answered, total, per = polls.counts(p)
    money = polls.money(p)
    rows = []
    for i, t in enumerate(p.targets, 1):
        ans = p.options[t.answer] if t.answer is not None and t.answer < len(p.options) else "—"
        when = f"{to_local(t.answered_at, tz):%d.%m %H:%M}" if t.answered_at else ""
        paid = f"<td>{polls.rub(p.amount) if t.paid else ''}</td>" if p.amount else ""
        rows.append(f"<tr><td class=n>{i}</td><td>{e(t.name)}</td><td>{e(kids.get(t.user_id, ''))}</td>"
                    f"<td>{e(ans)}</td><td>{when}</td>{paid}<td class=sig></td></tr>")
    head_money = "<th>Сдано</th>" if p.amount else ""
    totals = " · ".join(f"{e(o)} — {n}" for o, n in zip(p.options, per))
    money_line = (f"<div>Собрано {polls.rub(money['collected'])} из {polls.rub(money['expected'])} "
                  f"(подтверждено {money['paid']} из {money['participants']})</div>") if money else ""
    body = (f"<div class=page><h1>Ведомость: {e(p.title)}</h1>"
            f"<p class=meta>{e(school_name + ' · ' if school_name else '')}{e(cls)} · "
            f"классный руководитель {e(teacher)}<br>Срок: {e(fmt_dt(p.deadline, tz))}"
            f"{' · сбор закрыт' if p.closed else ''}</p>"
            f"<table><thead><tr><th>№</th><th>Родитель (законный представитель)</th><th>Ребёнок</th><th>Ответ</th>"
            f"<th>Когда</th>{head_money}<th>Подпись</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
            f"<div class=tot>Ответили {answered} из {total}. {totals}{money_line}</div>"
            f"<div class=foot><span>Классный руководитель ____________ / {e(teacher)} /</span>"
            f"<span>«____» ____________ 20___ г.</span></div>"
            f"<p class=meta style=\"margin-top:18px;font-size:12px\">Ответы собраны ботом «Перемена» в MAX: "
            f"каждый ответ дан родителем лично, кнопкой в своём аккаунте.</p></div>")
    return _doc(f"Ведомость — {p.title}", body)


def _body(p: Poll) -> str:
    """Текст сбора без первой строки, если она и есть заголовок (он уже напечатан крупно)."""
    first, _, rest = p.text.partition("\n")
    return rest.strip() if first.strip() == p.title.strip() else p.text


def _qr(link: str) -> str:
    from ..webapp.site_api import _qr_svg

    return _qr_svg(link)


async def blanks(p: Poll, only_missing: bool = False) -> str:
    school_name, cls, teacher, kids = await _context(p)
    c = ctx()
    link = c.start_link(f"poll_{p.id}") if c.bot_username else ""
    qr = _qr(link) if link else ""
    targets = [t for t in p.targets if not (only_missing and t.answer is not None)]
    if not targets:
        targets = [None]  # пустой бланк, если список класса ещё не известен
    opts = "".join(f"<span>☐ {e(o)}</span>" for o in p.options)
    cards = []
    for t in targets:
        parent = e(t.name) if t else "&nbsp;"
        child = e(kids.get(t.user_id, "")) if t else ""
        online = ""
        if t is not None and t.answer is not None and t.answer < len(p.options):
            online = f"<div class=online>Онлайн-ответ уже получен: «{e(p.options[t.answer])}»</div>"
        qr_box = (f"<div class=qr>{qr}<div>Ответить онлайн: наведите камеру — ответ сразу попадёт учителю</div></div>"
                  if qr else "")
        cards.append(
            f"<div class=blank><div class=txt><h2>{e(p.title)}</h2>"
            f"<div class=meta>{e(school_name + ' · ' if school_name else '')}{e(cls)} · {e(teacher)}</div>"
            f"<div class=body>{e(_body(p))}</div>"
            f"<div>Я, <span class=line>{parent}</span>, родитель (законный представитель)</div>"
            f"<div style=\"margin-top:6px\">обучающегося <span class=line>{child or '&nbsp;'}</span></div>"
            f"<div class=opts>{opts}</div>"
            f"<div>Подпись <span class=line style=\"min-width:120px\">&nbsp;</span> "
            f"Дата «____» ________ 20___ г.</div>{online}</div>{qr_box}</div>")
    return _doc(f"Бланки — {p.title}", "<div class=page>" + "".join(cards) + "</div>")
