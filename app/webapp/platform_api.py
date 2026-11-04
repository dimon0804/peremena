"""API мини-приложения и кабинета: регион (сбор сверху, сводка методиста) и открытая библиотека шаблонов.

Методист получает только агрегаты по школам и классам: ни ФИО родителей, ни их ответов в этих ответах API нет.
"""
from __future__ import annotations

import hmac
import io
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..models import User
from ..services import library, polls, region, school
from .api import _deadline, _sign, current_user, faq_out, teacher_user

router = APIRouter(prefix="/api")
EXPORT_TTL = 24 * 3600


# ====================================================================== библиотека
@router.get("/library")
async def library_list(q: str = Query("", max_length=200), kind: str | None = Query(None, pattern="^(poll|faq)$"),
                       topic: str | None = None, grades: str | None = None,
                       sort: str = Query("popular", pattern="^(popular|new)$"),
                       user: User = Depends(current_user)):
    items = await library.search_items(user.user_id, q=q, kind=kind, topic=topic, grades=grades, sort=sort, limit=60)
    return {"items": [library.out(it, user.user_id) for it in items],
            "topics": [{"key": k, "label": v} for k, v in library.TOPICS.items()],
            "grades": [{"key": k, "label": v} for k, v in library.GRADES.items()],
            "moderator": region.is_platform_admin(user.user_id)}


class LibraryIn(BaseModel):
    kind: str = Field("poll", pattern="^(poll|faq)$")
    title: str = Field("", max_length=300)
    text: str = Field("", max_length=3500)
    template: str = "consent"
    options: list[str] = []
    topic: str = ""
    grades: str = ""
    poll_id: int | None = None   # опубликовать свой сбор
    faq_id: int | None = None    # опубликовать свою карточку справочника
    confirm: bool = False        # публиковать, несмотря на предупреждения о возможных именах


@router.post("/library")
async def library_publish(body: LibraryIn, user: User = Depends(current_user)):
    if body.poll_id:
        it, blocks, warns = await library.publish_poll(user.user_id, body.poll_id, confirm=body.confirm)
    elif body.faq_id:
        it, blocks, warns = await library.publish_faq(user.user_id, body.faq_id, confirm=body.confirm)
    else:
        r = await school.roles(user.user_id)
        if not r.is_staff and not (await region.membership(user.user_id))[0]:
            raise HTTPException(403, "Публиковать шаблоны могут учителя")
        it, blocks, warns = await library.publish(user.user_id, body.kind, body.title, body.text, body.template,
                                                  body.options, body.topic, body.grades, confirm=body.confirm)
    if it is None:
        # не опубликовано: блокирующие причины (телефон, почта) или предупреждения, которые нужно подтвердить
        return {"ok": False, "blocks": blocks, "warnings": warns}
    return {"ok": True, "item": library.out(it, user.user_id), "warnings": warns}


@router.post("/library/{item_id}/take")
async def library_take(item_id: int, user: User = Depends(current_user)):
    it, res = await library.take(user.user_id, item_id)
    if it is None:
        raise HTTPException(404, "Шаблон не найден")
    out = {"item": library.out(it, user.user_id)}
    if it.kind == "faq":
        from ..services import faq

        e = await faq.get_entry(res["faq_id"])
        out["faq"] = faq_out(e, user.user_id)
    else:
        out["draft"] = res
    return out


class ReportIn(BaseModel):
    reason: str = Field("", max_length=300)


@router.post("/library/{item_id}/report")
async def library_report(item_id: int, body: ReportIn, user: User = Depends(current_user)):
    res = await library.report(user.user_id, item_id, body.reason)
    if res == "missing":
        raise HTTPException(404, "Шаблон не найден")
    return {"status": res}


@router.delete("/library/{item_id}")
async def library_delete(item_id: int, user: User = Depends(current_user)):
    if await library.remove_own(user.user_id, item_id):
        return {"ok": True}
    if await library.moderate(user.user_id, item_id, "delete"):
        return {"ok": True}
    raise HTTPException(404, "Шаблон не найден")


class ModerateIn(BaseModel):
    action: str = Field(pattern="^(hide|show|delete)$")


@router.get("/library/moderation")
async def library_moderation(user: User = Depends(current_user)):
    if not region.is_platform_admin(user.user_id):
        raise HTTPException(403, "Только для администратора платформы")
    return [library.out(it) for it in await library.pending_moderation()]


@router.post("/library/{item_id}/moderate")
async def library_moderate(item_id: int, body: ModerateIn, user: User = Depends(current_user)):
    if not region.is_platform_admin(user.user_id):
        raise HTTPException(403, "Только для администратора платформы")
    it = await library.moderate(user.user_id, item_id, body.action)
    if it is None:
        raise HTTPException(404, "Шаблон не найден")
    return {"ok": True}


# ====================================================================== приписка к сбору сверху
class NoteIn(BaseModel):
    note: str = Field("", max_length=region.NOTE_MAX)


@router.put("/polls/{pid}/note")
async def poll_note(pid: int, body: NoteIn, user: User = Depends(teacher_user)):
    p, status = await region.set_note(pid, user.user_id, body.note)
    if status == "missing":
        raise HTTPException(404, "Сбор не найден или пришёл не сверху")
    if status == "closed":
        raise HTTPException(409, "Сбор уже закрыт")
    return {"status": status, "note": p.note}


# ====================================================================== регион
async def methodist(user: User = Depends(current_user)):
    reg, role = await region.membership(user.user_id)
    if reg is None:
        raise HTTPException(403, "Только для методиста управления образования")
    return user, reg


def region_info(reg) -> dict:
    return {"id": reg.id, "name": reg.name, "kind": reg.kind, "code": reg.code, "staff_code": reg.staff_code}


@router.get("/region")
async def region_dashboard(um=Depends(methodist)):
    user, reg = um
    return {**await region.dashboard(reg.id), "codes": region_info(reg)}


class RegionIn(BaseModel):
    name: str = Field(min_length=3, max_length=300)
    district: bool = False


@router.post("/region")
async def region_create(body: RegionIn, user: User = Depends(current_user)):
    if body.district:
        reg = await region.create_district(user.user_id, body.name)
        if reg is None:
            raise HTTPException(403, "Округ создаёт методист региона")
    else:
        if not region.can_create_region(user.user_id):
            raise HTTPException(403, "Регион создаёт администратор платформы")
        reg = await region.create_region(user.user_id, body.name)
    return region_info(reg)


class RegionJoinIn(BaseModel):
    code: str = Field(min_length=4, max_length=32)


@router.post("/region/join")
async def region_join(body: RegionJoinIn, user: User = Depends(current_user)):
    reg, res = await region.join_by_code(user.user_id, body.code)
    if res == "limit":
        raise HTTPException(429, "Слишком много попыток — попробуйте через час")
    if res == "not_admin":
        raise HTTPException(403, "Это код для школ — его вводит администратор школы")
    if reg is None:
        raise HTTPException(404, "Код не найден")
    return {"id": reg.id, "name": reg.name, "as": res}


class AssignIn(BaseModel):
    user_id: int


@router.post("/region/{region_id}/methodists")
async def region_assign(region_id: int, body: AssignIn, user: User = Depends(current_user)):
    if not await region.assign_methodist(user.user_id, region_id, body.user_id):
        raise HTTPException(403, "Назначать методистов может администратор платформы")
    return {"ok": True}


class RegionCampaignIn(BaseModel):
    template: str = "consent"
    text: str = Field(min_length=3, max_length=3000)
    options: list[str] = []
    deadline: str | None = None
    school_ids: list[int] = []
    grades: list[int] = []


@router.post("/region/campaigns")
async def region_launch(body: RegionCampaignIn, um=Depends(methodist)):
    user, reg = um
    if body.template not in region.REGION_TEMPLATES:
        raise HTTPException(400, "Этот вид сбора нельзя спустить сверху")
    if body.template == "choice" and not 2 <= len([o for o in body.options if o.strip()]) <= 10:
        raise HTTPException(400, "Нужно от 2 до 10 вариантов")
    if any(not 1 <= g <= 11 for g in body.grades):
        raise HTTPException(400, "Параллель — от 1 до 11")
    rc, info = await region.launch(user.user_id, reg.id, body.template, body.text, body.options,
                                   _deadline(body.deadline, user.tz), body.school_ids or None, body.grades or None)
    if rc is None or not info["classes"]:
        raise HTTPException(400, "В выбранных школах и параллелях нет ни одного подключённого класса")
    return {"campaign": await region.summary(rc), **info}


async def _rc(rc_id: int, user: User):
    rc = await region.get_campaign(rc_id)
    if rc is None or not await region.can_see(user.user_id, rc):
        raise HTTPException(404, "Сбор не найден")
    return rc


def region_export_url(rc) -> str:
    exp = int(time.time()) + EXPORT_TTL
    return f"/api/region/campaigns/{rc.id}/export.csv?exp={exp}&sig={_sign('region', rc.id, rc.region_id, exp)}"


@router.get("/region/campaigns/{rc_id}")
async def region_campaign(rc_id: int, um=Depends(methodist)):
    rc = await _rc(rc_id, um[0])
    return {**await region.summary(rc), "export_url": region_export_url(rc)}


@router.get("/region/campaigns/{rc_id}/export.csv")
async def region_export(rc_id: int, sig: str = Query(max_length=64), exp: int = 0):
    rc = await region.get_campaign(rc_id)
    if rc is None or exp < time.time() or \
            not hmac.compare_digest(sig.encode(), _sign("region", rc.id, rc.region_id, exp).encode()):
        raise HTTPException(404, "Ссылка устарела — откройте сводку и выгрузите заново")
    data = region.summary_csv(await region.summary(rc))
    return StreamingResponse(io.BytesIO(data), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="region_{rc.id}.csv"'})


@router.get("/platform/me")
async def platform_me(user: User = Depends(current_user)):
    """Роли уровня выше школы: методист и администратор платформы (для вкладок приложения)."""
    reg, role = await region.membership(user.user_id)
    r = await school.roles(user.user_id)
    school_region = None
    if r.school and r.school.region_id:
        sr = await region.get_region(r.school.region_id)
        school_region = {"id": sr.id, "name": sr.name} if sr else None
    return {"region": region_info(reg) if reg else None, "school_region": school_region,
            "platform_admin": region.is_platform_admin(user.user_id),
            "can_create_region": region.can_create_region(user.user_id),
            "templates": [k for k in region.REGION_TEMPLATES],
            "poll_templates": [k for k in polls.TEMPLATES if k != "money"]}
