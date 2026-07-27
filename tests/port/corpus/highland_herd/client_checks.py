"""Stands in for the app's own test module (named so pytest never collects it).

The two constructs that matter here are the sync ``TestClient`` and
``dependency_overrides``. Both are pervasive in a real suite, and both change
shape under wreath: the client is async, and the override — which in the
original is overwhelmingly used to swap the auth dependency — becomes
``TestClient.acting_as``.
"""
from fastapi.testclient import TestClient

from .api import current_rider
from .main import app

client = TestClient(app)


def a_rider():
    return {"handle": "bo", "roles": ["rider"]}


def check_list_requires_auth():
    app.dependency_overrides = {}
    response = client.get("/llamas/", params={"paddock_id": "p1"})
    assert response.status_code == 401


def check_list_returns_the_herd():
    app.dependency_overrides[current_rider] = a_rider
    response = client.get("/llamas/", params={"paddock_id": "p1"})
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def check_missing_llama_is_404():
    app.dependency_overrides[current_rider] = a_rider
    assert client.get("/llamas/nope").status_code == 404
