"""El servidor local solo atiende a la propia ventana de la app (CSRF y DNS rebinding)."""
import pytest

from server import app


@pytest.fixture()
def client():
    app.config["TESTING"] = True
    return app.test_client()


def test_host_ajeno_bloqueado(client):
    r = client.get("/api/config", base_url="http://atacante.example:5050")
    assert r.status_code == 403


def test_host_local_permitido(client):
    r = client.get("/api/config", base_url="http://127.0.0.1:5050")
    assert r.status_code == 200


def test_post_desde_otro_sitio_bloqueado(client):
    r = client.post(
        "/api/duplicates/remove",
        base_url="http://127.0.0.1:5050",
        headers={"Origin": "https://atacante.example", "Sec-Fetch-Site": "cross-site"},
    )
    assert r.status_code == 403


def test_post_con_origen_null_bloqueado(client):
    r = client.post("/api/clear", base_url="http://127.0.0.1:5050", headers={"Origin": "null"})
    assert r.status_code == 403


def test_post_mismo_origen_permitido(client):
    r = client.post(
        "/api/clear",
        base_url="http://127.0.0.1:5050",
        headers={"Origin": "http://127.0.0.1:5050", "Sec-Fetch-Site": "same-origin"},
    )
    assert r.status_code == 200
