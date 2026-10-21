"""HTTP-слой без токена: сервис поднимается, отдаёт мини-приложение и API в dev-режиме."""
from fastapi.testclient import TestClient


def test_service_boots_without_token(tmp_path, monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'h.db'}")
    monkeypatch.setenv("DEV_AUTH", "true")
    monkeypatch.setenv("REDIS_URL", "")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import app

    with TestClient(app) as client:
        h = client.get("/health").json()
        assert h["status"] == "ok" and h["db"] == "sqlite"
        page = client.get("/app/")
        assert page.status_code == 200 and "ПЕРЕМЕНА" in page.text
        assert client.get("/app/app.js").status_code == 200
        me = client.get("/api/me?dev_user=7").json()
        assert me["is_teacher"] is False and me["user"]["id"] == 7
        assert client.get("/api/me").status_code == 401
        assert client.get("/api/polls?dev_user=7").status_code == 403
        assert client.post("/webhook", json={}).status_code == 403
    get_settings.cache_clear()
