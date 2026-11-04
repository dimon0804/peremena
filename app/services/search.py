"""Детерминированный поиск без ИИ: нормализация, стемминг Snowball (russian) и триграммы.

Бот не угадывает смысл — он находит то, что учитель сохранил, и честно говорит,
насколько уверен. Порог «отвечаю сам» выше порога «предлагаю варианты».
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Generic, Iterable, TypeVar

import snowballstemmer

_stemmer = snowballstemmer.stemmer("russian")
_word_re = re.compile(r"[a-zа-я0-9]+", re.IGNORECASE)

STOPWORDS = {
    "а", "и", "в", "во", "на", "по", "с", "со", "к", "ко", "у", "о", "об", "от", "до", "за", "из", "для",
    "не", "ни", "но", "же", "ли", "бы", "то", "это", "этот", "эта", "эти", "так", "там", "тут", "вот",
    "что", "чего", "чем", "кто", "как", "какой", "какая", "какие", "какое", "какую", "каком",
    "когда", "где", "куда", "зачем", "почему", "сколько", "ли", "или", "уже", "еще", "ещё", "все", "всё",
    "мы", "вы", "я", "он", "она", "они", "мне", "нам", "вам", "нас", "вас", "его", "ее", "её", "их",
    "мой", "моя", "наш", "ваш", "твой", "свой", "сам", "самый", "очень", "можно", "нужно", "надо",
    "будет", "есть", "быть", "был", "была", "были", "пожалуйста", "подскажите", "скажите", "здравствуйте",
    "нужна", "нужен", "нужны",
    "добрый", "день", "вечер", "утро", "привет", "спасибо", "извините", "а", "ну", "бы", "кстати",
    "ребенок", "ребёнок", "дети", "детям", "детей",
}

QUESTION_WORDS = (
    "что", "когда", "где", "куда", "как", "какой", "какая", "какие", "какое", "сколько", "кто",
    "почему", "зачем", "во сколько", "подскажите", "скажите", "скиньте", "напомните", "можно ли",
)


# Разговорные формы школьных слов → полные: «физра» и «физкультура» должны совпадать
SYNONYMS = {
    "физра": "физкультура", "физре": "физкультуре", "физру": "физкультуру", "физры": "физкультуры",
    "физрук": "физкультура", "матеша": "математика", "матешу": "математику", "матеше": "математике",
    "англ": "английский", "инглиш": "английский", "родсобрание": "собрание",
    "домашка": "домашнее", "домашку": "домашнее", "дз": "домашнее",
}


def normalize(text: str) -> str:
    return (text or "").lower().replace("ё", "е").replace("физ-ра", "физра").replace("физ-ру", "физру")


def words(text: str) -> list[str]:
    return [SYNONYMS.get(w, w) for w in _word_re.findall(normalize(text))]


@lru_cache(maxsize=50000)
def stem(word: str) -> str:
    if word.isdigit() or len(word) <= 2:
        return word
    return _stemmer.stemWord(word)


def content_stems(text: str) -> list[str]:
    return [stem(w) for w in words(text) if w not in STOPWORDS]


def _significant(st: str) -> bool:
    return len(st) >= 3 or st.isdigit()


def trigrams(text: str) -> set[str]:
    t = " " + " ".join(words(text)) + " "
    return {t[i:i + 3] for i in range(len(t) - 2)} if len(t) >= 3 else set()


def trigram_similarity(a: str, b: str) -> float:
    ta, tb = trigrams(a), trigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def looks_like_question(text: str) -> bool:
    t = normalize(text).strip()
    if not t or len(t) > 400:
        return False
    if "?" in t:
        return True
    return t.startswith(QUESTION_WORDS)


def split_keywords(raw: str) -> list[str]:
    return [k.strip() for k in re.split(r"[,;\n]", raw or "") if k.strip()]


def auto_keywords(question: str, limit: int = 6) -> str:
    seen: list[str] = []
    for w in words(question):
        if w in STOPWORDS or len(w) < 3:
            continue
        if w not in seen:
            seen.append(w)
    return ", ".join(seen[:limit])


T = TypeVar("T")


@dataclass
class Hit(Generic[T]):
    item: T
    score: float


UNSURE_CAP = 0.5  # потолок оценки, если половина значимых слов вопроса не нашлась (ниже порога «отвечаю сам»)


@lru_cache(maxsize=20000)
def _doc_features(keywords: str, body: str) -> tuple[frozenset, frozenset, tuple, frozenset]:
    """Всё, что зависит только от карточки: основы слов текста и ключевых слов, ключевые фразы, триграммы.
    Карточки одни и те же от вопроса к вопросу — считаем один раз (кэш по тексту: правка карточки
    даёт новый ключ). Без кэша поиск по общей базе школы из 500 карточек занимал ~14 мс процессора
    на вопрос — всё это время процесс не принимает другие события."""
    doc_stems = frozenset(content_stems(body))
    phrases = []
    kw_stems: set[str] = set()
    for kw in split_keywords(keywords):
        ks = content_stems(kw)
        kw_stems.update(ks)
        if ks:
            phrases.append((frozenset(ks), len(ks)))
    return doc_stems, frozenset(kw_stems), tuple(phrases), frozenset(trigrams(body + " " + keywords))


@lru_cache(maxsize=2000)
def _query_features(query: str) -> tuple[tuple, frozenset]:
    return tuple(content_stems(query)), frozenset(trigrams(query))


def score_text(query: str, keywords: str, body: str) -> float:
    """Оценка 0..1 совпадения запроса с карточкой (ключевые слова + текст вопроса)."""
    q, q_tri = _query_features(query)
    if not q:
        return 0.0
    qset = set(q)
    doc_stems, kw_stems, phrases, doc_tri = _doc_features(keywords or "", body or "")
    phrase_bonus = 0.0
    for ks, n in phrases:
        if ks <= qset:
            # ключевая фраза целиком есть в вопросе — сильный сигнал
            phrase_bonus = max(phrase_bonus, 0.35 if n > 1 else 0.25)
    all_stems = doc_stems | kw_stems
    overlap = len(qset & all_stems) / len(qset)
    kw_overlap = len(qset & kw_stems) / len(qset) if kw_stems else 0.0
    tri = len(q_tri & doc_tri) / len(q_tri | doc_tri) if q_tri and doc_tri else 0.0
    score = 0.55 * overlap + 0.25 * kw_overlap + 0.2 * tri + phrase_bonus
    # Штраф за значимые слова вопроса, которых нет в карточке: «форма на экскурсию» ≠ «форма на физкультуру».
    # Совпало одно слово из двух — это повод предложить вариант, но не отвечать уверенно.
    significant = {s for s in qset if _significant(s)}
    if significant:
        missing = len(significant - all_stems) / len(significant)
        score -= 0.4 * missing
        if missing >= 0.5 and len(significant) >= 2:
            score = min(score, UNSURE_CAP)
    return round(max(min(score, 1.0), 0.0), 3)


def rank(query: str, items: Iterable[T], keywords_of, body_of, limit: int = 5, min_score: float = 0.2) -> list[Hit[T]]:
    hits = [Hit(it, score_text(query, keywords_of(it), body_of(it))) for it in items]
    hits = [h for h in hits if h.score >= min_score]
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit]
