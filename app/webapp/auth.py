"""Проверка данных запуска мини-приложения MAX (https://dev.max.ru/docs/webapps/validation).

secret_key = HMAC_SHA256(key="WebAppData", msg=<токен бота>)
hash      = hex(HMAC_SHA256(key=secret_key, msg="k1=v1\\nk2=v2…"))  — пары без hash, отсортированы по ключу
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl, unquote

# Часы телефона могут спешить — небольшой запас для auth_date «из будущего»
CLOCK_SKEW_S = 5 * 60


class InitDataError(Exception):
    pass


def _check(pairs: list[tuple[str, str]], token: str) -> bool:
    hashes = [v for k, v in pairs if k == "hash"]
    if len(hashes) != 1:
        return False
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        return False
    data_check = "\n".join(f"{k}={v}" for k, v in sorted((k, v) for k, v in pairs if k != "hash"))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    # сравниваем байты: строка с не-ASCII символами в compare_digest даёт TypeError (и 500)
    return hmac.compare_digest(calc.encode(), hashes[0].lower().encode("utf-8", "replace"))


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict:
    """object_pairs_hook для json.loads: два одинаковых ключа (например, второй «id») — подделка."""
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise InitDataError("повторяющиеся поля в данных запуска")
    return dict(pairs)


def validate_init_data(init_data: str, token: str, max_age_s: int = 24 * 3600) -> dict:
    """Возвращает разобранные данные запуска или бросает InitDataError."""
    if not init_data or not token:
        raise InitDataError("нет данных запуска")
    raw = init_data.strip()
    if raw.startswith("#"):
        raw = raw[1:]
    # Иногда приходит весь фрагмент URL: WebAppData=<urlencoded>&WebAppPlatform=...
    if raw.startswith("WebAppData="):
        frag = dict(parse_qsl(raw, keep_blank_values=True))
        raw = frag.get("WebAppData", "")

    # Данные берём ровно из того представления пар, на котором сошлась подпись. Иначе подписанное
    # значение (например, имя с «%22,%22id%22:…») после повторного декодирования превращается в другие поля.
    raw_pairs = [tuple(p.split("=", 1)) if "=" in p else (p, "") for p in raw.split("&") if p]
    variants = (
        lambda: parse_qsl(raw, keep_blank_values=True),
        # платформа уже декодировала значения один раз — берём их как есть
        lambda: [(k, v) for k, v in raw_pairs],
        lambda: [(k, unquote(v)) for k, v in raw_pairs],
    )
    pairs = None
    for make in variants:
        candidate = make()
        if _check(candidate, token):
            pairs = candidate
            break
    if pairs is None:
        raise InitDataError("подпись не сошлась")

    data: dict = dict(pairs)
    try:
        auth_date = int(data.get("auth_date", ""))
    except ValueError:
        raise InitDataError("нет даты запуска")
    if auth_date > 10 ** 11:  # на случай миллисекунд
        auth_date //= 1000
    now = time.time()
    if auth_date <= 0 or auth_date > now + CLOCK_SKEW_S:
        raise InitDataError("неверная дата запуска")
    if max_age_s and now - auth_date > max_age_s:
        raise InitDataError("данные запуска устарели — откройте приложение заново")
    for key in ("user", "chat"):
        if key in data:
            try:
                data[key] = json.loads(data[key], object_pairs_hook=_no_duplicates)
            except ValueError:
                if key == "user":
                    raise InitDataError("нет пользователя")
    user = data.get("user")
    uid = user.get("id") if isinstance(user, dict) else None
    if isinstance(uid, bool) or not (isinstance(uid, int) or (isinstance(uid, str) and uid.isdecimal())) \
            or int(uid) <= 0:
        raise InitDataError("нет пользователя")
    return data
