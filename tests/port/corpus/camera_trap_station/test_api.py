"""Endpoint tests with framework-global dependency replacement."""

from fastapi.testclient import TestClient


def test_station_summary():
    app.dependency_overrides[current_ranger] = lambda: ranger
    client = TestClient(app)
    response = client.get("/station/summary")
    assert response.status_code == 200
