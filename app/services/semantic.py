"""Семантический поиск по справочнику — опционально, по умолчанию выключен (EMBEDDINGS=off).

Зачем. Детерминированный поиск (search.py) находит карточку по словам. Если родитель спросил другими
словами — «во сколько забирать с продлёнки», а у учителя карточка «До какого часа работает ГПД?», —
общих слов нет, и вопрос уходит учителю. Эмбеддинги cointegrated/rubert-tiny2 (312 чисел на фразу)
помогают такую карточку найти.

Чего здесь нет. Генерации текста нет: бот по-прежнему отвечает только тем, что сохранил учитель.
Вектор лишь поднимает карточку в выдаче. Семантика может повысить оценку карточки, но не понизить;
уверенный ответ «по смыслу» бывает только при высоком косинусе и заметном отрыве от соседней карточки,
иначе — «возможно, вы имели в виду» или вопрос учителю, как раньше. В общий чат по смыслу бот
не отвечает никогда (оценка семантики ограничена ниже порога группы).

Как устроено:
* Embedder — интерфейс «список фраз → список нормированных векторов». Основная реализация —
  OnnxEmbedder: onnxruntime + tokenizers, без torch (модель в ONNX кладётся в EMBEDDINGS_MODEL_DIR
  при сборке образа с WITH_EMBEDDINGS=1, см. scripts/export_rubert_onnx.py). Запасная — sentence-transformers.
* Векторы карточек — в таблице faq_embeddings: всегда байтами (float32), а на PostgreSQL с pgvector
  ещё и в столбце embedding vector(312) с индексом HNSW; тогда косинус считает сама база.
* Карточку пересчитываем в фоне пачками после сохранения/правки; раз в несколько минут лидер
  досчитывает пропущенное (демо, перенос базы, сбой модели). Вручную — `python -m app embeddings reindex`.
* Любая ошибка модели или базы → поиск молча работает как без эмбеддингов (и пишет в журнал).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from array import array
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Protocol, Sequence, TypeVar

from . import search

log = logging.getLogger(__name__)

DIM = 312                      # размер вектора rubert-tiny2; под него столбец vector(312) в PostgreSQL
PG_COLUMN = "embedding"
PG_INDEX = "ix_faq_embeddings_hnsw"
BATCH = 32                     # карточек за один прогон модели
DEBOUNCE = 0.5                 # с, ждём, пока учитель досохранит пачку карточек
QUERY_CACHE = 512              # векторов вопросов в памяти процесса


class Embedder(Protocol):
    """Превращает фразы в нормированные векторы одинаковой длины. Вызывается в отдельном потоке."""
    name: str
    dim: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


# ---------------------------------------------------------------- реализации

def _pooling_of(model_dir: Path, default: str = "cls") -> str:
    """Способ свёртки токенов в вектор — как в конфиге sentence-transformers модели (у rubert-tiny2 — CLS)."""
    cfg = model_dir / "1_Pooling" / "config.json"
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    if data.get("pooling_mode_cls_token"):
        return "cls"
    if data.get("pooling_mode_mean_tokens"):
        return "mean"
    return default


class OnnxEmbedder:
    """rubert-tiny2 в ONNX: onnxruntime + tokenizers, без гигабайта torch: в процессе модель занимает около 190 МиБ, вектор вопроса — 2–3 мс.

    В папке модели нужны model.onnx (выход — last_hidden_state) и tokenizer.json; 1_Pooling/config.json —
    по желанию (по нему выбирается CLS или среднее)."""

    def __init__(self, model_dir: str | Path, name: str = "rubert-tiny2", threads: int = 1,
                 max_tokens: int = 128) -> None:
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        d = Path(model_dir)
        self._np = np
        self.tok = Tokenizer.from_file(str(d / "tokenizer.json"))
        self.tok.enable_truncation(max_length=max_tokens)
        pad_id = self.tok.token_to_id("[PAD]")
        self.tok.enable_padding(pad_id=pad_id if pad_id is not None else 0, pad_token="[PAD]")
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, threads)  # модель маленькая: одного ядра хватает, остальные — боту
        opts.inter_op_num_threads = 1
        self.sess = ort.InferenceSession(str(d / "model.onnx"), opts, providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.sess.get_inputs()}
        self.pooling = _pooling_of(d)
        self.name = name
        self.dim = int(self.sess.get_outputs()[0].shape[-1] or DIM)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        np = self._np
        if not texts:
            return []
        enc = self.tok.encode_batch([t or "" for t in texts])
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        feeds = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.inputs:
            feeds["token_type_ids"] = np.array([e.type_ids for e in enc], dtype=np.int64)
        hidden = self.sess.run(None, feeds)[0]
        if self.pooling == "mean":
            m = mask[..., None].astype(hidden.dtype)
            vec = (hidden * m).sum(axis=1) / np.clip(m.sum(axis=1), 1e-9, None)
        else:
            vec = hidden[:, 0, :]
        vec = vec / np.clip(np.linalg.norm(vec, axis=1, keepdims=True), 1e-12, None)
        return vec.astype(np.float32).tolist()


class SentenceTransformerEmbedder:
    """Запасной вариант: sentence-transformers (тянет torch — образ тяжелее примерно на гигабайт)."""

    def __init__(self, model: str, name: str = "rubert-tiny2", threads: int = 1) -> None:
        import torch
        from sentence_transformers import SentenceTransformer

        torch.set_num_threads(max(1, threads))
        self.model = SentenceTransformer(model, device="cpu")
        self.name = name
        self.dim = int(self.model.get_sentence_embedding_dimension() or DIM)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        return self.model.encode(list(texts), normalize_embeddings=True, convert_to_numpy=True).tolist()


def load_embedder(settings) -> Embedder:
    """Модель по настройкам: EMBEDDINGS_BACKEND=auto|onnx|st, EMBEDDINGS_MODEL_DIR, EMBEDDINGS_MODEL."""
    backend = (settings.embeddings_backend or "auto").strip().lower()
    d = Path(settings.embeddings_model_dir)
    name = settings.embeddings_model.rsplit("/", 1)[-1]
    if backend in ("auto", "onnx") and (d / "model.onnx").exists():
        return OnnxEmbedder(d, name=name, threads=settings.embeddings_threads)
    if backend == "onnx":
        raise FileNotFoundError(f"нет {d / 'model.onnx'} — соберите образ с WITH_EMBEDDINGS=1 "
                                "или запустите scripts/export_rubert_onnx.py")
    # sentence-transformers: папка с моделью, если она есть, иначе имя на Hugging Face (скачает сам)
    src = str(d) if (d / "config.json").exists() else settings.embeddings_model
    return SentenceTransformerEmbedder(src, name=name, threads=settings.embeddings_threads)


# ---------------------------------------------------------------- состояние процесса

@dataclass
class Thresholds:
    """Пороги косинуса. Значения по умолчанию — осторожные, подбираются замером
    (loadtest/eval_semantic.py) на настоящей модели: EMBEDDINGS_ANSWER_COS, _SUGGEST_COS, _MARGIN."""
    answer_cos: float = 0.76   # от него карточку можно отдать как уверенный ответ
    suggest_cos: float = 0.61  # от него карточку можно предложить «возможно, вы имели в виду»
    margin: float = 0.08       # уверенный ответ по смыслу — только с таким отрывом от второй карточки


_embedder: Embedder | None = None
_thresholds = Thresholds()
_pg_vector: bool = False       # в базе есть столбец vector(312) — косинус считает PostgreSQL
_pending: set[int] = set()
_task: asyncio.Task | None = None
_qcache: OrderedDict[str, list[float]] = OrderedDict()
_warned: set[str] = set()


def enabled() -> bool:
    return _embedder is not None


def embedder() -> Embedder | None:
    return _embedder


def thresholds() -> Thresholds:
    return _thresholds


def set_embedder(e: Embedder | None, th: Thresholds | None = None, pg_vector: bool = False) -> None:
    """Включить (или выключить, e=None) семантический поиск в этом процессе. Для тестов и init()."""
    global _embedder, _thresholds, _pg_vector, _task
    _embedder = e
    _thresholds = th or Thresholds()
    _pg_vector = bool(pg_vector and e is not None and e.dim == DIM)
    _pending.clear()
    _qcache.clear()
    _task = None


def _warn_once(key: str, msg: str, *args) -> None:
    if key not in _warned:
        _warned.add(key)
        log.warning(msg, *args)


async def init(settings) -> bool:
    """Старт сервиса: при EMBEDDINGS=on загружает модель и проверяет pgvector. Ошибка — не падение,
    а работа без эмбеддингов с записью в журнал."""
    if not settings.embeddings_on:
        set_embedder(None)
        return False
    try:
        e = await asyncio.to_thread(load_embedder, settings)
    except Exception as exc:  # noqa: BLE001 — нет пакетов или модели: бот работает как раньше
        log.error("EMBEDDINGS=on, но модель не загрузилась (%s) — семантический поиск выключен. "
                  "Нужен образ с WITH_EMBEDDINGS=1 или pip install -r requirements-ml.txt", exc)
        set_embedder(None)
        return False
    th = Thresholds(settings.embeddings_answer_cos, settings.embeddings_suggest_cos, settings.embeddings_margin)
    pg = False
    from .. import db

    eng = db.engine()
    if eng is not None and eng.dialect.name == "postgresql":
        try:
            async with eng.begin() as conn:
                pg = await conn.run_sync(ensure_pgvector)
        except Exception as exc:  # noqa: BLE001
            log.warning("pgvector недоступен (%s) — векторы храню байтами, косинус считаю в процессе", exc)
    set_embedder(e, th, pg_vector=pg)
    log.info("Семантический поиск включён: %s (%d измерений), pgvector: %s, пороги %s",
             e.name, e.dim, "да" if _pg_vector else "нет", th)
    return True


# ---------------------------------------------------------------- pgvector

def ensure_pgvector(conn) -> bool:
    """Синхронно, в открытой транзакции: на PostgreSQL с доступным расширением vector — столбец
    faq_embeddings.embedding vector(312) и индекс HNSW (косинус). Возвращает, есть ли столбец.

    Безопасно где угодно: на SQLite и на PostgreSQL без расширения ничего не делает. Каждый шаг —
    в точке сохранения: ошибка (нет прав на CREATE EXTENSION, старый pgvector без HNSW) не обрывает
    общую транзакцию миграций. Вызывается из миграции 0010 и при старте с EMBEDDINGS=on
    (если базу перевели на образ с pgvector уже после миграции)."""
    if conn.dialect.name != "postgresql":
        return False
    installed = conn.exec_driver_sql("SELECT 1 FROM pg_extension WHERE extname = 'vector'").scalar()
    if not installed:
        available = conn.exec_driver_sql("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'").scalar()
        if not available:
            return False
        try:
            with conn.begin_nested():
                conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
        except Exception as exc:  # noqa: BLE001
            # соседний экземпляр мог поставить расширение одновременно с нами — тогда всё в порядке
            if not conn.exec_driver_sql("SELECT 1 FROM pg_extension WHERE extname = 'vector'").scalar():
                log.warning("CREATE EXTENSION vector не удался (%s) — векторы будут храниться байтами", exc)
                return False
    try:
        with conn.begin_nested():
            conn.exec_driver_sql(f"ALTER TABLE faq_embeddings ADD COLUMN IF NOT EXISTS {PG_COLUMN} vector({DIM})")
    except Exception as exc:  # noqa: BLE001
        if not _has_pg_column(conn):
            log.warning("столбец vector(%d) не добавлен (%s)", DIM, exc)
            return False
    try:
        with conn.begin_nested():
            conn.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS {PG_INDEX} ON faq_embeddings "
                                 f"USING hnsw ({PG_COLUMN} vector_cosine_ops)")
    except Exception as exc:  # noqa: BLE001 — pgvector < 0.5 без HNSW: поиск работает и без индекса
        log.warning("индекс HNSW не создан (%s) — поиск по столбцу vector работает и без него", exc)
    return True


def _has_pg_column(conn) -> bool:
    return bool(conn.exec_driver_sql(
        "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
        f"AND table_name = 'faq_embeddings' AND column_name = '{PG_COLUMN}'").scalar())


def _pg_literal(vec: Sequence[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in vec) + "]"


# ---------------------------------------------------------------- векторы

def card_text(question: str, keywords: str = "") -> str:
    """Что превращаем в вектор: вопрос карточки и ключевые слова учителя (в них часто синонимы)."""
    q = (question or "").strip()
    kw = (keywords or "").strip()
    return f"{q} ({kw})" if kw else q


def text_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def pack(vec: Sequence[float]) -> bytes:
    return array("f", vec).tobytes()


def unpack(raw: bytes) -> array:
    a = array("f")
    a.frombytes(raw)
    return a


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Косинус нормированных векторов — просто скалярное произведение (на всякий случай делим на нормы)."""
    if len(a) != len(b) or not len(a):
        return 0.0
    dot = math.fsum(x * y for x, y in zip(a, b))
    na = math.sqrt(math.fsum(x * x for x in a))
    nb = math.sqrt(math.fsum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _cosines(q: Sequence[float], rows: list[tuple[int, bytes]]) -> dict[int, float]:
    """Косинусы вопроса с кандидатами класса. Их десятки-сотни: numpy, если есть, иначе чистый Python."""
    try:
        import numpy as np
    except ImportError:
        return {i: cosine(q, unpack(raw)) for i, raw in rows}
    if not rows:
        return {}
    qv = np.asarray(q, dtype=np.float32)
    good = [(i, raw) for i, raw in rows if len(raw) == 4 * len(qv)]
    if not good:
        return {}
    m = np.frombuffer(b"".join(raw for _, raw in good), dtype=np.float32).reshape(len(good), len(qv))
    sims = m @ qv / np.clip(np.linalg.norm(m, axis=1) * (np.linalg.norm(qv) or 1.0), 1e-12, None)
    return {i: float(s) for (i, _), s in zip(good, sims)}


async def embed_query(text: str) -> list[float]:
    key = search.normalize(text).strip()[:500]
    if key in _qcache:
        _qcache.move_to_end(key)
        return _qcache[key]
    e = _embedder
    assert e is not None
    vec = (await asyncio.to_thread(e.embed, [key]))[0]
    _qcache[key] = vec
    while len(_qcache) > QUERY_CACHE:
        _qcache.popitem(last=False)
    return vec


async def similarities(query: str, entries: list) -> dict[int, float]:
    """Косинус вопроса с каждой карточкой-кандидатом (FaqEntry), у которой есть свежий вектор.

    Кандидаты — уже отобранные по классу и школе (faq.candidates_for_chats): чужие карточки
    сюда не попадают ни в одном режиме. Карточки без вектора или с устаревшим — в очередь на пересчёт."""
    from sqlalchemy import bindparam, select, text as sql

    from ..db import session
    from ..models import FaqEmbedding

    e = _embedder
    if e is None or not entries:
        return {}
    want = {x.id: text_hash(card_text(x.question, x.keywords)) for x in entries}
    q = await embed_query(query)
    result: dict[int, float] = {}
    async with session() as s:
        if _pg_vector:
            stmt = sql(
                f"SELECT faq_id, text_hash, 1 - ({PG_COLUMN} <=> CAST(CAST(:q AS text) AS vector)) AS cos "
                f"FROM faq_embeddings WHERE faq_id IN :ids AND model = :m AND {PG_COLUMN} IS NOT NULL"
            ).bindparams(bindparam("ids", expanding=True))
            rows = (await s.execute(stmt, {"q": _pg_literal(q), "ids": list(want), "m": e.name})).all()
            for fid, h, cos in rows:
                if want.get(fid) == h:
                    result[fid] = float(cos)
        else:
            rows = (await s.execute(select(FaqEmbedding.faq_id, FaqEmbedding.text_hash, FaqEmbedding.vec).where(
                FaqEmbedding.faq_id.in_(list(want)), FaqEmbedding.model == e.name))).all()
            result = _cosines(q, [(fid, raw) for fid, h, raw in rows if want.get(fid) == h])
    missing = [fid for fid in want if fid not in result]
    if missing:
        schedule(missing)
    return result


# ---------------------------------------------------------------- гибридное ранжирование

def semantic_score(cos: float, th: Thresholds, suggest: float, answer: float, ceiling: float) -> float:
    """Косинус → оценка по шкале детерминированного поиска (0..1, те же пороги справочника).

    ниже suggest_cos — 0 (семантика молчит); suggest_cos..answer_cos — зона «возможно, вы имели в виду»;
    выше answer_cos — зона уверенного ответа, но не выше ceiling (ниже порога автоответа в общем чате)."""
    if cos < th.suggest_cos:
        return 0.0
    if cos < th.answer_cos:
        k = (cos - th.suggest_cos) / max(th.answer_cos - th.suggest_cos, 1e-9)
        return suggest + k * (answer - suggest) * 0.999
    k = (cos - th.answer_cos) / max(1.0 - th.answer_cos, 1e-9)
    return min(answer + k * (ceiling - answer), ceiling)


T = TypeVar("T")


def hybrid_rank(query: str, items: Iterable[T], cos: dict[int, float], *, key_of: Callable[[T], int],
                keywords_of: Callable[[T], str], body_of: Callable[[T], str], limit: int, min_score: float,
                answer: float, ceiling: float, th: Thresholds | None = None) -> list[search.Hit[T]]:
    """Детерминированная оценка + косинус. Итог — максимум из двух: семантика может только поднять карточку.

    Уверенный ответ, который дала одна семантика (слова не совпали), требует отрыва top-1 от top-2
    по косинусу не меньше margin: две похожие карточки («форма на физкультуру» и «форма на экскурсию»)
    — повод предложить варианты, а не угадывать."""
    th = th or _thresholds
    items = list(items)
    ranked = sorted(cos.values(), reverse=True)
    top = ranked[0] if ranked else -1.0
    second = ranked[1] if len(ranked) > 1 else -1.0
    clear_leader = top - second >= th.margin
    hits: list[search.Hit[T]] = []
    for it in items:
        det = search.score_text(query, keywords_of(it), body_of(it))
        c = cos.get(key_of(it))
        score = det
        if c is not None:
            sem = semantic_score(c, th, min_score, answer, ceiling)
            if sem >= answer and det < answer and not (c >= top and clear_leader):
                sem = min(sem, answer - 0.001)  # не уверены, какая из близких — только предлагаем
            score = max(det, round(sem, 3))
        hits.append(search.Hit(it, round(score, 3), cos=None if c is None else round(c, 4)))
    hits = [h for h in hits if h.score >= min_score]
    hits.sort(key=lambda h: (h.score, h.cos or 0.0), reverse=True)
    return hits[:limit]


# ---------------------------------------------------------------- индексация

def schedule(ids: Iterable[int]) -> None:
    """Пересчитать векторы карточек в фоне (после сохранения или правки). Без включённой модели — ничего."""
    global _task
    if _embedder is None:
        return
    _pending.update(int(i) for i in ids)
    if not _pending:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _task is None or _task.done():
        _task = loop.create_task(_drain(), name="embeddings")


async def _drain() -> None:
    await asyncio.sleep(DEBOUNCE)
    while _pending and _embedder is not None:
        batch = [_pending.pop() for _ in range(min(BATCH, len(_pending)))]
        try:
            await index_ids(batch)
        except Exception as exc:  # noqa: BLE001 — досчитает catch_up
            _warn_once("drain", "Не удалось посчитать векторы карточек: %s", exc)
            return


async def wait_idle() -> None:
    """Дождаться фоновой индексации (тесты, команда reindex)."""
    while _task is not None and not _task.done():
        await asyncio.wait({_task}, timeout=5)


async def index_ids(ids: list[int], force: bool = False) -> int:
    """Считает и сохраняет векторы карточек ids. Свежие (тот же текст и модель) пропускает, если не force."""
    from sqlalchemy import select, text as sql
    from sqlalchemy.exc import IntegrityError

    from ..db import session, utcnow
    from ..models import FaqEmbedding, FaqEntry

    e = _embedder
    if e is None or not ids:
        return 0
    async with session() as s:
        cards = (await s.execute(select(FaqEntry.id, FaqEntry.question, FaqEntry.keywords)
                                 .where(FaqEntry.id.in_(ids)))).all()
        have = {fid: (m, h) for fid, m, h in (await s.execute(
            select(FaqEmbedding.faq_id, FaqEmbedding.model, FaqEmbedding.text_hash)
            .where(FaqEmbedding.faq_id.in_(ids)))).all()}
    todo = []
    for fid, question, keywords in cards:
        t = card_text(question, keywords)
        h = text_hash(t)
        if force or have.get(fid) != (e.name, h):
            todo.append((fid, t, h))
    if not todo:
        return 0
    vecs = await asyncio.to_thread(e.embed, [t for _, t, _ in todo])
    now = utcnow()
    rows = [(fid, h, v) for (fid, _, h), v in zip(todo, vecs)]

    async def save(chunk) -> None:
        async with session() as s:
            for fid, h, v in chunk:
                await s.merge(FaqEmbedding(faq_id=fid, model=e.name, text_hash=h, dim=len(v), vec=pack(v),
                                           updated_at=now))
            if _pg_vector:
                await s.flush()
                for fid, _, v in chunk:
                    await s.execute(sql(f"UPDATE faq_embeddings SET {PG_COLUMN} = CAST(CAST(:v AS text) AS vector) "
                                        "WHERE faq_id = :id"), {"v": _pg_literal(v), "id": fid})

    try:
        await save(rows)
    except IntegrityError:
        # соседний процесс записал ту же карточку одновременно (или карточку удалили) — по одной
        for r in rows:
            try:
                await save([r])
            except IntegrityError:
                pass
    return len(rows)


async def stale_ids(limit: int = 500) -> list[int]:
    """Карточки без вектора, с вектором другой модели или изменённые после подсчёта."""
    from sqlalchemy import or_, select, text as sql

    from ..db import session
    from ..models import FaqEmbedding, FaqEntry

    e = _embedder
    if e is None:
        return []
    async with session() as s:
        stmt = (select(FaqEntry.id).outerjoin(FaqEmbedding, FaqEmbedding.faq_id == FaqEntry.id)
                .where(or_(FaqEmbedding.faq_id.is_(None), FaqEmbedding.model != e.name,
                           FaqEmbedding.updated_at < FaqEntry.updated_at))
                .order_by(FaqEntry.id).limit(limit))
        ids = list((await s.scalars(stmt)).all())
        if _pg_vector and len(ids) < limit:  # перенесли базу копированием — байты есть, столбца vector нет
            more = (await s.execute(sql(f"SELECT faq_id FROM faq_embeddings WHERE {PG_COLUMN} IS NULL "
                                        "ORDER BY faq_id LIMIT :n"), {"n": limit - len(ids)})).scalars().all()
            ids += [i for i in more if i not in ids]
    return ids


async def drop_orphans() -> int:
    """Векторы удалённых карточек (на SQLite внешние ключи не каскадят) — удаляем."""
    from sqlalchemy import delete, select

    from ..db import session
    from ..models import FaqEmbedding, FaqEntry

    async with session() as s:
        r = await s.execute(delete(FaqEmbedding).where(FaqEmbedding.faq_id.not_in(select(FaqEntry.id))))
        return r.rowcount or 0


async def catch_up(limit: int = 500) -> int:
    """Задача лидера раз в несколько минут: досчитать пропущенное, убрать векторы удалённых карточек."""
    if _embedder is None:
        return 0
    await drop_orphans()
    done = 0
    ids = await stale_ids(limit)
    for i in range(0, len(ids), BATCH):
        done += await index_ids(ids[i:i + BATCH], force=True)
    return done


async def reindex(all_: bool = False, progress: Callable[[int, int], None] | None = None) -> int:
    """Команда `python -m app embeddings reindex [--all]`: пересчитать пропущенное или всё."""
    from sqlalchemy import func, select

    from ..db import session
    from ..models import FaqEntry

    if _embedder is None:
        return 0
    await drop_orphans()
    done = 0
    if all_:
        async with session() as s:
            total = await s.scalar(select(func.count()).select_from(FaqEntry)) or 0
        last = 0
        while True:
            async with session() as s:
                ids = list((await s.scalars(select(FaqEntry.id).where(FaqEntry.id > last)
                                            .order_by(FaqEntry.id).limit(BATCH))).all())
            if not ids:
                break
            done += await index_ids(ids, force=True)
            last = ids[-1]
            if progress:
                progress(done, total)
        return done
    while True:
        ids = await stale_ids(BATCH * 8)
        if not ids:
            return done
        n = 0
        for i in range(0, len(ids), BATCH):
            n += await index_ids(ids[i:i + BATCH], force=True)
        done += n
        if progress:
            progress(done, done + len(await stale_ids(1)))
        if n == 0:  # ничего не записалось (карточки удалили на ходу) — не крутимся впустую
            return done


async def status() -> dict:
    from sqlalchemy import func, select

    from ..db import session
    from ..models import FaqEmbedding, FaqEntry

    async with session() as s:
        cards = await s.scalar(select(func.count()).select_from(FaqEntry)) or 0
        vectors = await s.scalar(select(func.count()).select_from(FaqEmbedding)) or 0
    stale = len(await stale_ids(100000)) if _embedder is not None else None
    return {"enabled": enabled(), "model": getattr(_embedder, "name", None), "dim": getattr(_embedder, "dim", None),
            "pgvector": _pg_vector, "cards": cards, "vectors": vectors, "stale": stale,
            "thresholds": _thresholds.__dict__}
