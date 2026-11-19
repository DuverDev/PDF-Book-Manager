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


def _epub_malicioso(tmp_path):
    import zipfile
    ruta = tmp_path / "libro.epub"
    with zipfile.ZipFile(ruta, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr(
            "OEBPS/cap1.xhtml",
            '<html><body><p>Capitulo 1</p><script>fetch("/api/clear",{method:"POST"})</script>'
            '<meta http-equiv="refresh" content="0;url=https://atacante.example"></body></html>',
        )
    return ruta


def test_vista_previa_epub_aislada(client, tmp_path, monkeypatch):
    """SEC-085: el HTML de un EPUB no puede ejecutar scripts con acceso a la API local."""
    from types import SimpleNamespace
    import server

    ruta = _epub_malicioso(tmp_path)
    libro = SimpleNamespace(
        filepath=str(ruta), filename="libro.epub",
        real_title='<img src=x onerror="alert(1)">', real_author="<b>Autor</b>",
    )
    monkeypatch.setitem(server._settings, "currentFolder", str(tmp_path))
    monkeypatch.setattr(server, "_books_state", {"abc": libro})

    r = client.get("/api/books/abc/pdf", base_url="http://127.0.0.1:5050")
    assert r.status_code == 200
    csp = r.headers["Content-Security-Policy"]
    assert csp.startswith("sandbox") and "default-src 'none'" in csp
    assert "allow-scripts" not in csp and "allow-same-origin" not in csp
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    cuerpo = r.get_data(as_text=True)
    assert '<img src=x onerror="alert(1)">' not in cuerpo
    assert "&lt;img src=x" in cuerpo and "&lt;b&gt;Autor&lt;/b&gt;" in cuerpo
    assert "http-equiv" not in cuerpo
