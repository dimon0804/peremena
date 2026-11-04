"""Семантический поиск (EMBEDDINGS=on) — на поддельном детерминированном эмбеддере, без настоящей модели.

Поддельная модель: слово → основа → «понятие» по маленькому словарю синонимов → номер измерения
по md5. Фразы с общими понятиями получают близкие векторы, как перефразировки у rubert-tiny2.
Проверяем логику вокруг модели: гибридное ранжирование, пороги, отрыв, фоновую индексацию,
устаревшие векторы и то, что при EMBEDDINGS=off всё ровно как раньше."""
from __future__ import annotations

import hashlib
import math

import pytest
from sqlalchemy import inspect, select

from app import db
from app.bot.router import handle_update
from app.db import session
from app.models import FaqEmbedding, FaqEntry, User
from app.services import answers, faq, search, semantic

from .conftest import CHAT_ID, PARENTS, TEACHER

DIMS = 256
_GROUPS = {
    "ГПД": ("продлёнка", "продлёнки", "продлёнку", "гпд", "продленка"),
    "КОНЕЦ": ("забирать", "забрать", "работает", "заканчивается", "закрывается"),
    "ВРЕМЯ": ("часа", "час", "время", "времени"),
    "ФОРМА": ("форма", "форму", "одежда", "одеваться", "надеть"),
    "ФИЗРА": ("физкультуру", "физкультура", "физкультуры"),
    "ЭКСКУРСИЯ": ("экскурсию", "экскурсия", "музей"),
    "СОБРАНИЕ": ("собрание", "собрания", "встреча"),
    "РОДИТЕЛИ": ("родительское", "родителей"),
    "ДЕНЬГИ": ("сдавать", "деньги", "оплата", "оплатить", "стоит"),
}
SKIP = {"какого", "какой", "во", "сколько", "до", "с"}  # служебные слова поддельная модель не видит
SYN = {search.stem(w.replace("ё", "е")): concept for concept, ws in _GROUPS.items() for w in ws}


class FakeEmbedder:
    name = "fake-synonyms"
    dim = DIMS

    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    def _one(self, text: str) -> list[float]:
        v = [0.0] * DIMS
        concepts = {SYN.get(search.stem(w), search.stem(w)) for w in search.words(text)
                    if w not in search.STOPWORDS and w not in SKIP}
        for concept in concepts:  # мешок понятий без повторов: ключевые слова не удваивают вес
            v[int(hashlib.md5(concept.encode()).hexdigest(), 16) % DIMS] = 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def embed(self, texts):
        self.calls += 1
        if self.fail:
            raise RuntimeError("модель упала")
        return [self._one(t) for t in texts]


TH = semantic.Thresholds(answer_cos=0.75, suggest_cos=0.5, margin=0.1)


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(semantic, "DEBOUNCE", 0.05)
    e = FakeEmbedder()
    semantic.set_embedder(e, TH)
    yield e
    semantic.set_embedder(None)


def run(loop, coro):
    return loop.run_until_complete(coro)


async def _cards(teacher_id: int = TEACHER["user_id"]):
    gpd = await faq.add_entry(teacher_id, "До какого часа работает ГПД?", "Группа продлённого дня — до 18:00.")
    form = await faq.add_entry(teacher_id, "Какая форма на физкультуру?", "Белая футболка, тёмные шорты, кеды.")
    meet = await faq.add_entry(teacher_id, "Когда родительское собрание?", "В четверг в 18:30, кабинет 21.")
    await semantic.wait_idle()
    return gpd, form, meet


async def _all(teacher_id: int = TEACHER["user_id"]):
    return await faq.list_entries(teacher_id)


PARAPHRASE = "Во сколько забирать с продлёнки?"


# ---------------------------------------------------------------- выключено — как раньше

def test_disabled_is_exactly_deterministic(env):
    loop, _, _ = env
    semantic.set_embedder(None)
    run(loop, _cards())
    entries = run(loop, _all())
    for q in (PARAPHRASE, "Какая форма на физру?", "когда собрание родителей", "сколько сдавать на экскурсию"):
        smart = run(loop, faq.rank_smart(q, entries))
        plain = faq.rank(q, entries)
        assert [(h.item.id, h.score, h.cos) for h in smart] == [(h.item.id, h.score, h.cos) for h in plain]
    # и векторы не считаются, таблица пустая
    assert run(loop, _vectors()) == {}


def test_paraphrase_is_not_found_without_embeddings(env):
    """Отправная точка: перефразировка без общих слов уходит учителю."""
    loop, _, _ = env
    semantic.set_embedder(None)
    run(loop, _cards())
    hits = run(loop, faq.rank_smart(PARAPHRASE, run(loop, _all())))
    assert not hits or hits[0].score < faq.ANSWER_SCORE


# ---------------------------------------------------------------- включено

async def _vectors():
    async with session() as s:
        return {r.faq_id: r for r in (await s.scalars(select(FaqEmbedding))).all()}


def test_cards_are_indexed_in_background(env, fake):
    loop, _, _ = env
    gpd, form, meet = run(loop, _cards())
    vecs = run(loop, _vectors())
    assert set(vecs) == {gpd.id, form.id, meet.id}
    assert all(v.model == "fake-synonyms" and v.dim == DIMS and len(v.vec) == 4 * DIMS for v in vecs.values())
    # три карточки подряд обычно уходят одной пачкой; под нагрузкой фон может стартовать раньше третьей
    assert 1 <= fake.calls <= 3


def test_paraphrase_is_found_and_answer_is_teachers_text(env, fake):
    loop, _, _ = env
    gpd, form, _ = run(loop, _cards())
    hits = run(loop, faq.rank_smart(PARAPHRASE, run(loop, _all())))
    assert hits[0].item.id == gpd.id and hits[0].score >= faq.ANSWER_SCORE
    assert hits[0].cos is not None and hits[0].cos >= TH.answer_cos
    # семантика никогда не дотягивает до автоответа в общем чате
    assert hits[0].score < faq.GROUP_SCORE
    # чужая тема не всплывает
    assert form.id not in {h.item.id for h in hits}


def test_private_answer_flow_uses_embeddings(env, fake):
    """Родитель спрашивает в личке другими словами — получает ответ учителя слово в слово."""
    loop, api_, _ = env
    run(loop, handle_update({"update_type": "bot_added", "timestamp": 1, "chat_id": CHAT_ID, "user": TEACHER,
                             "is_channel": False}))

    async def working_hours():
        async with session() as s:
            (await s.get(User, TEACHER["user_id"])).quiet_enabled = False
    run(loop, working_hours())
    gpd, _, _ = run(loop, _cards())
    a = run(loop, answers.answer(PARENTS[0]["user_id"], PARAPHRASE))
    assert a.kind == "faq" and a.entry.id == gpd.id
    assert "Группа продлённого дня — до 18:00." in a.text and "сохранено учителем" in a.text

    semantic.set_embedder(None)
    b = run(loop, answers.answer(PARENTS[1]["user_id"], PARAPHRASE))
    assert b.kind != "faq"  # без эмбеддингов — как раньше: ответа нет, вопрос можно передать учителю


def test_deterministic_score_is_never_lowered(env, fake):
    loop, _, _ = env
    run(loop, _cards())
    entries = run(loop, _all())
    q = "Какая форма на физру?"
    det = {h.item.id: h.score for h in faq.rank(q, entries)}
    smart = {h.item.id: h.score for h in run(loop, faq.rank_smart(q, entries))}
    for fid, s in det.items():
        assert smart[fid] >= s


def test_stale_vector_is_ignored_and_recomputed(env, fake):
    loop, _, _ = env
    gpd, _, _ = run(loop, _cards())
    old = run(loop, _vectors())[gpd.id].text_hash
    # учитель переписал вопрос — старый вектор больше не про эту карточку
    run(loop, faq.update_entry(gpd.id, TEACHER["user_id"], question="Сколько стоит экскурсия в музей?"))
    entries = run(loop, _all())
    cos = run(loop, semantic.similarities(PARAPHRASE, entries))  # до пересчёта: вектор устарел — не используем
    assert gpd.id not in cos
    run(loop, semantic.wait_idle())
    assert run(loop, _vectors())[gpd.id].text_hash != old
    hits = run(loop, faq.rank_smart(PARAPHRASE, run(loop, _all())))
    assert not hits or hits[0].item.id != gpd.id or hits[0].score < faq.ANSWER_SCORE


def test_delete_removes_vector(env, fake):
    loop, _, _ = env
    gpd, _, _ = run(loop, _cards())
    assert run(loop, faq.delete_entry(gpd.id, TEACHER["user_id"]))
    assert gpd.id not in run(loop, _vectors())


def test_model_failure_falls_back_to_words(env, fake):
    loop, _, _ = env
    run(loop, _cards())
    entries = run(loop, _all())
    semantic._qcache.clear()
    fake.fail = True
    q = "Какая форма на физру?"
    smart = run(loop, faq.rank_smart(q, entries))
    assert [(h.item.id, h.score) for h in smart] == [(h.item.id, h.score) for h in faq.rank(q, entries)]


def test_catch_up_indexes_cards_saved_past_the_service_and_drops_orphans(env, fake):
    """Карточки, записанные мимо faq.add_entry (демо, перенос базы), досчитывает задача лидера."""
    loop, _, _ = env

    async def direct():
        async with session() as s:
            e = FaqEntry(teacher_id=TEACHER["user_id"], question="Какая форма на физкультуру?", keywords="",
                         answer="Спортивная.")
            s.add(e)
            await s.flush()
            s.add(FaqEmbedding(faq_id=99999, model="fake-synonyms", text_hash="x", dim=1, vec=b"\0\0\0\0",
                               updated_at=db.utcnow()))
            return e.id
    try:
        fid = run(loop, direct())
    except Exception:  # PostgreSQL проверяет внешний ключ — сироту там не создать, проверяем без неё
        fid = None
    if fid is None:
        async def direct_pg():
            async with session() as s:
                e = FaqEntry(teacher_id=TEACHER["user_id"], question="Какая форма на физкультуру?", keywords="",
                             answer="Спортивная.")
                s.add(e)
                await s.flush()
                return e.id
        fid = run(loop, direct_pg())
    assert run(loop, semantic.stale_ids()) == [fid]
    assert run(loop, semantic.catch_up()) == 1
    vecs = run(loop, _vectors())
    assert fid in vecs and 99999 not in vecs
    assert run(loop, semantic.stale_ids()) == []
    assert run(loop, semantic.reindex(all_=True)) == 1


# ---------------------------------------------------------------- чистая функция ранжирования

ITEMS = [(1, "Какая форма на физкультуру?"), (2, "Какая форма на экскурсию?"), (3, "Когда собрание?")]


def _hr(query, cos):
    return semantic.hybrid_rank(query, ITEMS, cos, key_of=lambda x: x[0], keywords_of=lambda x: "",
                                body_of=lambda x: x[1], limit=3, min_score=faq.SUGGEST_SCORE,
                                answer=faq.ANSWER_SCORE, ceiling=faq.GROUP_SCORE - 0.01, th=TH)


def test_two_close_cards_are_only_suggested():
    """Косинус высокий у двух карточек сразу — не угадываем, а предлагаем обе."""
    hits = _hr("что надеть ребёнку", {1: 0.86, 2: 0.83, 3: 0.1})
    assert [h.item[0] for h in hits[:2]] == [1, 2]
    assert all(h.score < faq.ANSWER_SCORE for h in hits)
    assert hits[0].score >= faq.SUGGEST_SCORE


def test_clear_leader_is_answered():
    hits = _hr("что надеть ребёнку", {1: 0.86, 2: 0.55, 3: 0.1})
    assert hits[0].item[0] == 1 and faq.ANSWER_SCORE <= hits[0].score < faq.GROUP_SCORE


def test_low_cosine_adds_nothing():
    hits = _hr("что надеть ребёнку", {1: 0.3, 2: 0.2, 3: 0.1})
    assert hits == []


def test_semantic_score_is_monotonic_and_capped():
    xs = [semantic.semantic_score(c / 100, TH, faq.SUGGEST_SCORE, faq.ANSWER_SCORE, faq.GROUP_SCORE - 0.01)
          for c in range(0, 101)]
    assert xs == sorted(xs) and max(xs) < faq.GROUP_SCORE
    assert semantic.semantic_score(TH.suggest_cos - 0.01, TH, 0.3, 0.55, 0.84) == 0.0
    assert semantic.semantic_score(TH.answer_cos, TH, 0.3, 0.55, 0.84) >= 0.55


def test_pack_and_cosine_roundtrip():
    v = FakeEmbedder()._one("До какого часа работает ГПД?")
    assert semantic.cosine(v, semantic.unpack(semantic.pack(v))) == pytest.approx(1.0, abs=1e-6)
    got = semantic._cosines(v, [(7, semantic.pack(v))])
    assert got[7] == pytest.approx(1.0, abs=1e-5)


# ---------------------------------------------------------------- схема

def test_migration_creates_table_without_pg_extras_on_sqlite(env):
    loop, _, _ = env

    async def schema():
        async with db.engine().connect() as conn:
            def cols(c):
                insp = inspect(c)
                return ({x["name"] for x in insp.get_columns("faq_embeddings")},
                        {i["name"] for i in insp.get_indexes("faq_embeddings")})
            got = await conn.run_sync(cols)
            pg = await conn.run_sync(semantic.ensure_pgvector)
            return got, pg, conn.dialect.name
    (cols, idx), pg, dialect = run(loop, schema())
    assert {"faq_id", "model", "text_hash", "dim", "vec", "updated_at"} <= cols
    if dialect == "sqlite":
        assert "embedding" not in cols and not pg  # на SQLite — только байты, и ничего не ломается


def test_settings_default_off():
    from app.config import Settings

    assert Settings(_env_file=None).embeddings_on is False
    assert Settings(_env_file=None, embeddings="on").embeddings_on is True


def test_init_without_packages_keeps_bot_working(env, tmp_path):
    """EMBEDDINGS=on, а модели нет — сервис стартует, поиск работает по словам."""
    loop, _, c = env
    from app.config import Settings

    s = Settings(_env_file=None, embeddings="on", embeddings_backend="onnx", embeddings_model_dir=str(tmp_path))
    assert run(loop, semantic.init(s)) is False
    assert semantic.enabled() is False
