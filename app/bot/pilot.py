"""Бот: /metrics — метрики пилота для администратора платформы и завуча.

Администратор платформы (ADMIN_MAX_IDS) видит всю платформу без демо-песочниц, завуч — свою школу.
Классному руководителю показываем его собственное время ответа — то же, что в /stats."""
from __future__ import annotations

from ..services import chats, metrics
from .common import send


async def show_metrics(uid: int) -> None:
    scopes = await metrics.scopes_for(uid)
    if not scopes:
        if await chats.is_teacher(uid):
            a = await metrics.answer_time(("teacher", uid), 30)
            await send(uid, "⏱ <b>Время ответа родителям, 30 дней</b>\n\n" + "\n".join(metrics.answer_lines(a))
                       + "\n\n<i>Удержание учителей видят завуч (по своей школе) и администратор платформы.</i>")
        else:
            await send(uid, "Метрики пилота видят завуч школы и администратор платформы.")
        return
    for scope, title in scopes:
        await send(uid, metrics.render(await metrics.report(scope), title))
