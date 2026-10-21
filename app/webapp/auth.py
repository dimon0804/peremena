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
    return hmac.compare_digest(calc, hashes[0].lower())


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

    decoded = parse_qsl(raw, keep_blank_values=True)
    ok = _check(decoded, token)
    if not ok:
        # вариант, когда платформа уже декодировала значения один раз, а мы — второй
        raw_pairs = [tuple(p.split("=", 1)) if "=" in p else (p, "") for p in raw.split("&") if p]
        ok = _check([(k, v) for k, v in raw_pairs], token) or _check([(k, unquote(v)) for k, v in raw_pairs], token)
    if not ok:
        raise InitDataError("подпись не сошлась")

    data = dict(decoded)
    try:
        auth_date = int(data.get("auth_date", "0"))
    except ValueError:
        auth_date = 0
    if auth_date > 10 ** 11:  # на случай миллисекунд
        auth_date //= 1000
    if max_age_s and auth_date and time.time() - auth_date > max_age_s:
        raise InitDataError("данные запуска устарели — откройте приложение заново")
    for key in ("user", "chat"):
        if key in data:
            try:
                data[key] = json.loads(data[key])
            except ValueError:
                pass
    if not isinstance(data.get("user"), dict) or not data["user"].get("id"):
        raise InitDataError("нет пользователя")
    return data
