"""Справочник не отвечает уверенно на чужую тему, а разговорные слова понимает."""
from app.services import faq, search

CARD = ("форма, физкультуру", "Какая форма на физкультуру?")


def test_other_topic_is_not_answered_confidently():
    s = search.score_text("Какая форма на экскурсию?", *CARD)
    assert s < faq.ANSWER_SCORE  # максимум — «возможно, вы имели в виду»
    assert s < faq.GROUP_SCORE


def test_colloquial_subject_matches():
    assert search.score_text("Какая форма на физру?", *CARD) >= faq.GROUP_SCORE
    assert search.score_text("Какая форма на физ-ру?", *CARD) >= faq.GROUP_SCORE


def test_group_threshold_is_strict():
    assert faq.GROUP_SCORE >= 0.85
