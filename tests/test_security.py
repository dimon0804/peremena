"""Регрессии по аудиту безопасности: подмена входа, чужие данные, лимиты, заголовки."""
import hashlib
import hmac
import json
import time

import pytest

from app.webapp.auth import InitDataError, validate_init_data

TOKEN = "123:abc"


def _sign(fields: dict, token: str = TOKEN) -> str:
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    return hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()


def _raw(fields: dict, token: str = TOKEN) -> str:
    """Неэнкоденная строка данных запуска — так её иногда отдаёт платформа."""
    return "&".join(f"{k}={v}" for k, v in {**fields, "hash": _sign(fields, token)}.items())


# ---------------------------------------------------------------- данные запуска мини-приложения
def test_init_data_cannot_smuggle_foreign_id():
    # Имя подписано как есть, но после повторного декодирования дало бы второй «id» в JSON
    user = '{"id":5,"first_name":"x%22,%22id%22:100,%22z%22:%22"}'
    fields = {"auth_date": str(int(time.time())), "user": user}
    data = validate_init_data(_raw(fields), TOKEN)
    assert data["user"]["id"] == 5 and data["user"]["first_name"] == 'x%22,%22id%22:100,%22z%22:%22'


def test_init_data_rejects_duplicate_json_keys():
    user = '{"id":5,"first_name":"x","id":100}'
    fields = {"auth_date": str(int(time.time())), "user": user}
    with pytest.raises(InitDataError):
        validate_init_data(_raw(fields), TOKEN)


@pytest.mark.parametrize("auth_date", [None, "0", "abc", str(int(time.time()) + 3600)])
def test_init_data_requires_sane_auth_date(auth_date):
    fields = {"user": json.dumps({"id": 5}, separators=(",", ":"))}
    if auth_date is not None:
        fields["auth_date"] = auth_date
    with pytest.raises(InitDataError):
        validate_init_data(_raw(fields), TOKEN)


def test_init_data_non_ascii_hash_is_rejected_not_crash():
    fields = {"auth_date": str(int(time.time())), "user": '{"id":5}'}
    raw = "&".join(f"{k}={v}" for k, v in fields.items()) + "&hash=ж" * 1
    with pytest.raises(InitDataError):
        validate_init_data(raw, TOKEN)
