"""Оценка семантического поиска: сколько вопросов родителей бот закрывает сам с эмбеддингами и без.

    python loadtest/eval_semantic.py                         # модель из EMBEDDINGS_MODEL_DIR (как у сервиса)
    python loadtest/eval_semantic.py --model-dir /srv/models/rubert-tiny2 --sweep --json out.json
    python loadtest/eval_semantic.py --fake                  # без модели: проверить, что скрипт работает

Набор — 15 карточек справочника и 54 вопроса родителей. Вопросы составлены вручную: часть почти дословная,
часть — живые перефразировки («во сколько забирать с продлёнки» ↔ «до какого часа работает ГПД»),
8 вопросов — про то, чего в справочнике нет (там правильный исход — передать учителю, а не угадать).
Это не выборка настоящих вопросов родителей; цифры показывают, как ведёт себя поиск на таких формулировках.

Исходы вопроса — как в боте (app/services/answers.py, личка):
  ответ   — лучшая карточка набрала ANSWER_SCORE: бот отвечает текстом учителя;
  вариант — набрала SUGGEST_SCORE: «возможно, вы имели в виду», дальше родитель выбирает или передаёт учителю;
  учителю — ничего не нашлось, вопрос уходит учителю.
Главные числа: верных ответов (top-1 = нужная карточка и ответ уверенный), неверных уверенных ответов
(должно быть 0 — это ответ не на тот вопрос) и доля ушедших учителю.

--sweep перебирает пороги EMBEDDINGS_ANSWER_COS и EMBEDDINGS_MARGIN и подсказывает, какие поставить.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services import faq, search, semantic  # noqa: E402


@dataclass
class Card:
    id: int
    question: str
    keywords: str
    answer: str


_CARDS = [
    ("До какого часа работает ГПД?", "Группа продлённого дня работает до 18:00, забирать можно с 15:00."),
    ("Какая форма на физкультуру?", "Белая футболка, тёмные шорты или спортивный костюм, кеды или кроссовки."),
    ("Когда родительское собрание?", "В четверг в 18:30, кабинет 21."),
    ("Сколько сдавать на экскурсию в музей?", "По 650 ₽, до пятницы, через бота — кнопка «Перевёл»."),
    ("Расписание звонков", "1 урок 8:30–9:15, 2 урок 9:25–10:10, 3 урок 10:30–11:15, 4 урок 11:35–12:20."),
    ("Как получить справку для бассейна?", "Справку выдаёт педиатр в поликлинике, нужна отметка о дерматологе."),
    ("Как оплатить питание в столовой?", "Пополнить карту питания в приложении банка, обед — 145 ₽ в день."),
    ("Нужна ли сменная обувь?", "Да, сменная обувь обязательна, в мешке с подписью."),
    ("Когда начинаются каникулы?", "Осенние каникулы с 27 октября по 4 ноября."),
    ("Какие учебники купить?", "Все учебники выдаёт библиотека, покупать нужно только рабочие тетради."),
    ("Где посмотреть оценки?", "В электронном дневнике (Сферум / МЭШ), вход через Госуслуги."),
    ("Во сколько начинаются уроки?", "Первый урок в 8:30, приходить к 8:15."),
    ("Можно ли приносить телефон в школу?", "Можно, но на уроках телефон выключен и лежит в портфеле."),
    ("Как записаться в кружок?", "Список кружков — у завуча по воспитательной работе, запись до 15 сентября."),
    ("Когда будет медосмотр?", "Медосмотр в ноябре, дату сообщим за неделю."),
]
CARDS = [Card(i, q, search.auto_keywords(q), a) for i, (q, a) in enumerate(_CARDS, 1)]

# (вопрос родителя, номер карточки или None — в справочнике такого нет)
QUESTIONS: list[tuple[str, int | None]] = [
    ("Во сколько забирать с продлёнки?", 1),
    ("до скольки продленка сегодня", 1),
    ("Продлёнка до какого времени?", 1),
    ("Можно забрать ребёнка из группы продлённого дня в 17:30?", 1),
    ("До какого часа работает группа продлённого дня?", 1),
    ("Что надеть на физру?", 2),
    ("В чём заниматься на физкультуре?", 2),
    ("какая нужна спортивная форма", 2),
    ("Нужны ли кроссовки на урок физкультуры?", 2),
    ("Когда собрание для родителей?", 3),
    ("Во сколько родительское собрание в четверг?", 3),
    ("Будет ли встреча с родителями на этой неделе?", 3),
    ("Родсобрание когда?", 3),
    ("Сколько стоит поездка в музей?", 4),
    ("Какую сумму сдавать на экскурсию?", 4),
    ("На экскурсию деньги кому отдавать?", 4),
    ("Когда заканчивается третий урок?", 5),
    ("Во сколько перемена после второго урока?", 5),
    ("Скиньте расписание звонков", 5),
    ("Где делают справку в бассейн?", 6),
    ("Какая справка нужна для плавания?", 6),
    ("Как пополнить карту на обеды?", 7),
    ("Куда платить за еду в школе?", 7),
    ("Сколько стоит обед в столовой?", 7),
    ("питание как оплатить", 7),
    ("Сменку брать обязательно?", 8),
    ("Можно ходить в уличной обуви?", 8),
    ("Ребёнку нужны сменные тапочки?", 8),
    ("С какого числа осенние каникулы?", 9),
    ("Когда отдыхаем?", 9),
    ("Когда последний учебный день в четверти?", 9),
    ("Какие книги нужно купить к школе?", 10),
    ("Учебники выдают в библиотеке или покупать самим?", 10),
    ("Как узнать оценки ребёнка?", 11),
    ("Где электронный дневник?", 11),
    ("Не вижу отметки за неделю, где смотреть?", 11),
    ("К какому времени приводить ребёнка утром?", 12),
    ("Первый урок во сколько?", 12),
    ("Когда начало занятий утром?", 12),
    ("Ребёнку можно брать мобильник?", 13),
    ("Телефоны на уроках разрешены?", 13),
    ("Какие есть секции после уроков?", 14),
    ("Хотим на робототехнику, куда обращаться?", 14),
    ("Как попасть в кружок рисования?", 14),
    ("Когда врачи будут осматривать детей?", 15),
    ("медосмотр в каком месяце?", 15),
    # в справочнике такого нет — правильно передать учителю
    ("Когда будет контрольная по математике?", None),
    ("Можно перенести пересдачу?", None),
    ("Как зовут нового учителя английского?", None),
    ("Где найти потерянную куртку?", None),
    ("Будет ли школьная ёлка?", None),
    ("Какая форма на экскурсию?", None),
    ("Сколько стоит школьная форма?", None),
    ("Прививки когда ставят?", None),
]


class HashEmbedder:
    """--fake: мешок основ слов, разложенный по md5. Синонимов не знает — только для проверки скрипта."""
    name = "fake-hash"
    dim = 256

    def embed(self, texts):
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for st in search.content_stems(t):
                v[int(hashlib.md5(st.encode()).hexdigest(), 16) % self.dim] = 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


def outcome(hits) -> tuple[str, int | None, float, float | None]:
    if not hits:
        return "учителю", None, 0.0, None
    top = hits[0]
    kind = "ответ" if top.score >= faq.ANSWER_SCORE else "вариант"
    return kind, top.item.id, top.score, top.cos


def run_mode(mode: str, cos_by_q: dict | None, th: semantic.Thresholds | None) -> list[dict]:
    rows = []
    for q, exp in QUESTIONS:
        if mode == "off":
            hits = faq.rank(q, CARDS)
        else:
            hits = semantic.hybrid_rank(q, CARDS, cos_by_q[q], key_of=lambda c: c.id, keywords_of=lambda c: c.keywords,
                                        body_of=lambda c: c.question, limit=3, min_score=faq.SUGGEST_SCORE,
                                        answer=faq.ANSWER_SCORE, ceiling=faq.GROUP_SCORE - 0.01, th=th)
        kind, got, score, cos = outcome(hits)
        rows.append({"q": q, "expected": exp, "kind": kind, "top": got, "score": score, "cos": cos,
                     "in_top3": exp is not None and exp in [h.item.id for h in hits]})
    return rows


def summary(rows: list[dict]) -> dict:
    known = [r for r in rows if r["expected"] is not None]
    unknown = [r for r in rows if r["expected"] is None]
    right = sum(r["kind"] == "ответ" and r["top"] == r["expected"] for r in known)
    wrong = sum(r["kind"] == "ответ" and r["top"] != r["expected"] for r in rows)
    suggested = sum(r["kind"] == "вариант" for r in rows)
    suggested_ok = sum(r["kind"] == "вариант" and r["in_top3"] for r in known)
    teacher = sum(r["kind"] == "учителю" for r in rows)
    not_closed = sum(r["kind"] != "ответ" for r in known)
    return {
        "вопросов": len(rows), "есть_в_справочнике": len(known), "нет_в_справочнике": len(unknown),
        "верный_ответ_top1": right, "top1_доля": round(right / len(known), 3) if known else 0.0,
        "неверный_уверенный_ответ": wrong,
        "из_них_на_вопрос_без_карточки": sum(r["kind"] == "ответ" for r in unknown),
        "предложены_варианты": suggested, "варианты_с_нужной_карточкой": suggested_ok,
        "ушло_учителю_без_вариантов": teacher,
        "не_закрыто_ботом_доля": round(not_closed / len(known), 3) if known else 0.0,
    }


def print_summary(title: str, s: dict) -> None:
    print(f"\n== {title}")
    print(f"  верный уверенный ответ (top-1): {s['верный_ответ_top1']} из {s['есть_в_справочнике']} "
          f"({s['top1_доля']:.0%})")
    print(f"  неверный уверенный ответ:       {s['неверный_уверенный_ответ']} "
          f"(на вопросы без карточки: {s['из_них_на_вопрос_без_карточки']} из {s['нет_в_справочнике']})")
    print(f"  «возможно, вы имели в виду»:    {s['предложены_варианты']} "
          f"(нужная карточка среди вариантов: {s['варианты_с_нужной_карточкой']})")
    print(f"  ушло учителю без вариантов:     {s['ушло_учителю_без_вариантов']}")
    print(f"  не закрыто ботом сразу:         {s['не_закрыто_ботом_доля']:.0%} вопросов, на которые карточка есть")


def main() -> int:
    ap = argparse.ArgumentParser(description="оценка семантического поиска по справочнику")
    ap.add_argument("--model-dir", default=None, help="папка модели (по умолчанию EMBEDDINGS_MODEL_DIR)")
    ap.add_argument("--backend", default=None, help="auto | onnx | st")
    ap.add_argument("--fake", action="store_true", help="без модели — проверить сам скрипт")
    ap.add_argument("--answer-cos", type=float, default=None)
    ap.add_argument("--suggest-cos", type=float, default=None)
    ap.add_argument("--margin", type=float, default=None)
    ap.add_argument("--sweep", action="store_true", help="перебрать пороги")
    ap.add_argument("--verbose", action="store_true", help="исход каждого вопроса")
    ap.add_argument("--json", default=None, help="сохранить результаты в файл")
    args = ap.parse_args()

    from app.config import Settings

    over = {"embeddings": "on"}
    if args.model_dir:
        over["embeddings_model_dir"] = args.model_dir
    if args.backend:
        over["embeddings_backend"] = args.backend
    s = Settings(**over)
    th = semantic.Thresholds(args.answer_cos if args.answer_cos is not None else s.embeddings_answer_cos,
                             args.suggest_cos if args.suggest_cos is not None else s.embeddings_suggest_cos,
                             args.margin if args.margin is not None else s.embeddings_margin)

    t0 = time.perf_counter()
    emb = HashEmbedder() if args.fake else semantic.load_embedder(s)
    load_s = time.perf_counter() - t0
    card_vecs = emb.embed([semantic.card_text(c.question, c.keywords) for c in CARDS])
    t0 = time.perf_counter()
    q_vecs = [emb.embed([search.normalize(q).strip()[:500]])[0] for q, _ in QUESTIONS]  # по одному, как в боте
    per_q_ms = (time.perf_counter() - t0) / len(QUESTIONS) * 1000
    cos_by_q = {q: {c.id: semantic.cosine(qv, cv) for c, cv in zip(CARDS, card_vecs)}
                for (q, _), qv in zip(QUESTIONS, q_vecs)}

    print(f"Модель: {emb.name}, {emb.dim} измерений; загрузка {load_s:.1f} с; вектор вопроса {per_q_ms:.1f} мс")
    print(f"Карточек: {len(CARDS)}, вопросов: {len(QUESTIONS)}; пороги: {th}")
    off_rows = run_mode("off", None, None)
    on_rows = run_mode("on", cos_by_q, th)
    off, on = summary(off_rows), summary(on_rows)
    print_summary("EMBEDDINGS=off (только слова)", off)
    print_summary("EMBEDDINGS=on (слова + косинус)", on)

    if args.verbose:
        print("\nвопрос | ожидали | off: исход/карточка | on: исход/карточка/косинус")
        for a, b in zip(off_rows, on_rows):
            mark = "" if b["top"] == b["expected"] or (b["expected"] is None and b["kind"] != "ответ") else "  ←"
            print(f"  {a['q'][:48]:48} | {str(a['expected']):4} | {a['kind']:7} {str(a['top']):4} | "
                  f"{b['kind']:7} {str(b['top']):4} {b['cos'] if b['cos'] is not None else '-'}{mark}")

    sweep = []
    if args.sweep:
        print("\nПеребор порогов (EMBEDDINGS_SUGGEST_COS = ANSWER_COS − 0.15):")
        print("  answer_cos  margin  верных  неверных  учителю(без вариантов)")
        for a10 in range(55, 93, 3):
            a = a10 / 100
            for m in (0.0, 0.03, 0.05, 0.08):
                t = semantic.Thresholds(a, max(a - 0.15, 0.0), m)
                r = summary(run_mode("on", cos_by_q, t))
                sweep.append({"answer_cos": a, "suggest_cos": t.suggest_cos, "margin": m, **r})
                print(f"  {a:9.2f}  {m:6.2f}  {r['верный_ответ_top1']:6}  {r['неверный_уверенный_ответ']:8}  "
                      f"{r['ушло_учителю_без_вариантов']:6}")
        safe = [x for x in sweep if x["неверный_уверенный_ответ"] <= off["неверный_уверенный_ответ"]]
        if safe:
            best = max(safe, key=lambda x: (x["верный_ответ_top1"], x["answer_cos"], x["margin"]))
            print(f"\nБез лишних неверных ответов лучше всего: EMBEDDINGS_ANSWER_COS={best['answer_cos']:.2f} "
                  f"EMBEDDINGS_SUGGEST_COS={best['suggest_cos']:.2f} EMBEDDINGS_MARGIN={best['margin']:.2f} "
                  f"— верных {best['верный_ответ_top1']} из {best['есть_в_справочнике']}")
            print("На 54 вопросах это ориентир, а не закон: лучше взять порог чуть строже найденного.")

    if args.json:
        Path(args.json).write_text(json.dumps({
            "model": emb.name, "dim": emb.dim, "load_s": round(load_s, 2), "query_ms": round(per_q_ms, 2),
            "thresholds": th.__dict__, "off": off, "on": on, "sweep": sweep,
            "questions": [{"off": a, "on": b} for a, b in zip(off_rows, on_rows)],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nРезультаты: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
