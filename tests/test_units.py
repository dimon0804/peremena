import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from app.services import search
from app.timeutil import parse_deadline, parse_hours, to_local
from app.webapp.auth import InitDataError, validate_init_data


def test_question_detection():
    assert search.looks_like_question("Во сколько собрание?")
    assert search.looks_like_question("подскажите что задали по алгебре")
    assert not search.looks_like_question("Спасибо!")


def test_faq_scoring_prefers_right_card():
    cards = [
        ("собрание, родительское", "Во сколько родительское собрание?"),
        ("форма, одежда", "В какой форме приходить завтра?"),
    ]
    s1 = search.score_text("А собрание во сколько будет?", *cards[0])
    s2 = search.score_text("А собрание во сколько будет?", *cards[1])
    assert s1 > 0.55 > s2


def test_init_data_roundtrip():
    token = "123:abc"
    user = json.dumps({"id": 42, "first_name": "Мария"}, ensure_ascii=False, separators=(",", ":"))
    fields = {"auth_date": str(int(time.time())), "query_id": "q1", "user": user}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    raw = urlencode(fields)
    data = validate_init_data(raw, token)
    assert data["user"]["id"] == 42
    with pytest.raises(InitDataError):
        validate_init_data(raw, "other-token")


def test_deadline_parsing():
    tz = "Europe/Moscow"
    d = parse_deadline("завтра 9:00", tz)
    assert d is not None and to_local(d, tz).hour == 9
    assert parse_deadline("31.02", tz) is None
    assert parse_hours("8:30-17:00") == (510, 1020)
    assert parse_hours("19-8") is None
