import os
import socket
import sys
import threading
import time

if getattr(sys, "frozen", False):
    _BASE = os.path.dirname(sys.executable)
    _BUNDLE = sys._MEIPASS
else:
    _BASE = os.path.dirname(os.path.abspath(__file__))
    _BUNDLE = _BASE

os.environ["APP_BASE"] = _BASE
os.environ["APP_BUNDLE"] = _BUNDLE

from server import app


def find_free_port(default_port: int = 5050) -> int:
    for port in range(default_port, default_port + 25):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return default_port


PORT = find_free_port(5050)


def start_server():
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    t = threading.Thread(target=start_server, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{PORT}"
    try:
        import webview
        time.sleep(0.5)
        webview.create_window(
            "PDF Book Manager — Gestor de Libros PDF",
            url,
            width=1280,
            height=820,
            min_size=(920, 580),
        )
        webview.start()
    except ImportError:
        import webbrowser
        time.sleep(1.2)
        webbrowser.open(url)
        print(f"Abre {url} en tu navegador")
        print("Presiona Ctrl+C para detener")
        try:
            t.join()
        except KeyboardInterrupt:
            pass

