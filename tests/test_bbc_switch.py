"""Выключатель BBC Dashboard: `BBC_DASHBOARD_ENABLED=false` закрывает раздел целиком.

Открытым остаётся только `/bbc/status` — по нему фронт убирает плитку и
страницу раздела. Остальные двери отвечают 404, как будто раздела нет.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.bbc.config import bbc_settings
from app.main import app

BASE = "/api/v1/bbc"


def test_vyklyuchennyy_razdel_zakryt_krome_statusa(monkeypatch):
    monkeypatch.setattr(bbc_settings, "enabled", False)
    client = TestClient(app)

    status = client.get(f"{BASE}/status")
    assert status.status_code == 200
    assert status.json()["enabled"] is False

    for method, path in (
        ("post", "/auth/login"),
        ("get", "/auth/me"),
        ("get", "/dataset"),
        ("get", "/employees"),
    ):
        response = getattr(client, method)(f"{BASE}{path}", **({"json": {"username": "a", "password": "b"}} if method == "post" else {}))
        assert response.status_code == 404, f"{method.upper()} {path}: {response.status_code}"
        assert response.json()["detail"] == "BBC Dashboard выключен"


def test_vklyuchennyy_razdel_otvechaet_kak_prezhde(monkeypatch):
    monkeypatch.setattr(bbc_settings, "enabled", True)
    client = TestClient(app)
    assert client.get(f"{BASE}/status").json()["enabled"] is True
    # Вход с неверным паролем — обычный отказ, а не «раздел выключен».
    response = client.post(f"{BASE}/auth/login", json={"username": "нет-такого", "password": "x"})
    assert response.status_code == 401
