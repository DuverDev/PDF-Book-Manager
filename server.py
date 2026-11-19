from concurrent.futures import ThreadPoolExecutor
import html
import json
import mimetypes
import os
import subprocess
from pathlib import Path
from typing import Dict, List

from flask import Flask, render_template, jsonify, request, send_file

from pdf_engine import (
    BookRecord,
    MetadataCache,
    analyze_single_pdf,
    build_suggested_filename,
    detect_duplicates,
    format_size,
    query_google_books,
    query_open_library,
    quick_load_pdf,
    remove_duplicate_file,
    write_pdf_metadata_and_rename,
)

mimetypes.add_type("application/pdf", ".pdf")

_BASE = Path(os.environ.get("APP_BASE", str(Path(__file__).parent)))
_BUNDLE = Path(os.environ.get("APP_BUNDLE", str(Path(__file__).parent)))

app = Flask(
    __name__,
    template_folder=str(_BUNDLE / "templates"),
    static_folder=str(_BUNDLE),
    static_url_path="/static",
)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024

DEFAULT_BOOKS_DIR = _BASE / "libros"
SETTINGS_FILE = _BASE / "settings.json"
CACHE_FILE = _BASE / "metadata_cache.json"

_cache = MetadataCache(CACHE_FILE)
_books_state: Dict[str, BookRecord] = {}
_books_list: List[BookRecord] = []


def _load_settings() -> dict:
    DEFAULT_BOOKS_DIR.mkdir(parents=True, exist_ok=True)
    defaults = {
        "currentFolder": str(DEFAULT_BOOKS_DIR),
        "renamePattern": "{titulo} - {autor}",
        "useOnline": True,
        "recursive": False,
        "duplicateMode": "trash",
    }
    if SETTINGS_FILE.is_file():
        try:
            saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            defaults.update(saved)
        except Exception:
            pass
    if not Path(defaults["currentFolder"]).is_dir():
        defaults["currentFolder"] = str(DEFAULT_BOOKS_DIR)
    return defaults


def _save_settings(cfg: dict):
    try:
        SETTINGS_FILE.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


_settings = _load_settings()


def _compute_stats(books: List[BookRecord]) -> dict:
    total = len(books)
    analyzed_cnt = sum(1 for b in books if b.analyzed)
    dups = [b for b in books if b.is_duplicate]
    dup_groups = len({b.duplicate_group_id for b in books if b.duplicate_group_id})
    needs_rename = sum(1 for b in books if not b.is_duplicate and b.needs_rename)
    needs_meta = sum(1 for b in books if not b.is_duplicate and b.needs_metadata_update)
    total_bytes = sum(b.size_bytes for b in books)
    reclaim_bytes = sum(b.size_bytes for b in dups)
    return {
        "totalBooks": total,
        "analyzedCount": analyzed_cnt,
        "unanalyzedCount": total - analyzed_cnt,
        "duplicatesCount": len(dups),
        "duplicateGroupsCount": dup_groups,
        "needsRenameCount": needs_rename,
        "needsMetaCount": needs_meta,
        "totalSizeHuman": format_size(total_bytes),
        "reclaimableSizeHuman": format_size(reclaim_bytes),
    }


def _serialize_state() -> dict:
    return {
        "folder": _settings["currentFolder"],
        "settings": _settings,
        "books": [b.to_dict() for b in _books_list],
        "stats": _compute_stats(_books_list),
    }


def _update_settings_from_payload(data: dict):
    if "folder" in data and data["folder"]:
        cand = Path(data["folder"])
        if cand.is_dir():
            _settings["currentFolder"] = str(cand.resolve())
    if "renamePattern" in data and data["renamePattern"]:
        _settings["renamePattern"] = str(data["renamePattern"]).strip()
    if "useOnline" in data:
        _settings["useOnline"] = bool(data["useOnline"])
    if "recursive" in data:
        _settings["recursive"] = bool(data["recursive"])
    if "duplicateMode" in data and data["duplicateMode"] in ("trash", "folder", "delete"):
        _settings["duplicateMode"] = data["duplicateMode"]
    _save_settings(_settings)


def _list_pdf_files(folder: Path, recursive: bool) -> List[Path]:
    book_files: List[Path] = []
    valid_exts = {".pdf", ".epub"}
    if recursive:
        for p in sorted(folder.rglob("*")):
            if "_duplicados" in p.parts or p.name.startswith(".tmp_"):
                continue
            if p.is_file() and p.suffix.lower() in valid_exts:
                book_files.append(p)
    else:
        for p in sorted(folder.iterdir()):
            if p.is_file() and p.suffix.lower() in valid_exts and not p.name.startswith(".tmp_"):
                book_files.append(p)
    return book_files


# ==============================================================================
# SOLO LA PROPIA VENTANA DE LA APP
# El servidor escucha en 127.0.0.1, pero cualquier página web abierta en el
# navegador podría enviarle peticiones (CSRF) o apuntarle un dominio propio
# (DNS rebinding). Se exige Host local y, en las peticiones que cambian algo,
# que vengan del mismo origen.
# ==============================================================================
_LOCAL_HOSTS = {"127.0.0.1", "localhost"}


@app.before_request
def _solo_peticiones_locales():
    host = (request.host or "").rsplit(":", 1)[0].strip("[]").lower()
    if host not in _LOCAL_HOSTS:
        return jsonify({"error": "Host no permitido"}), 403
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("Origin")
        if origin is not None and origin.rstrip("/") != request.host_url.rstrip("/"):
            return jsonify({"error": "Origen no permitido"}), 403
        site = request.headers.get("Sec-Fetch-Site")
        if site is not None and site not in ("same-origin", "none"):
            return jsonify({"error": "Origen no permitido"}), 403
    return None


@app.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify({
        "settings": _settings,
        "defaultFolder": str(DEFAULT_BOOKS_DIR),
    })


@app.route("/api/set-folder", methods=["POST"])
def set_folder():
    data = request.get_json(silent=True) or {}
    folder_str = (data.get("folder") or "").strip()
    if not folder_str:
        return jsonify({"error": "Ruta de carpeta vacía"}), 400
    p = Path(folder_str)
    if not p.is_dir():
        return jsonify({"error": f"La carpeta no existe: {folder_str}"}), 404
    _settings["currentFolder"] = str(p.resolve())
    _save_settings(_settings)
    return jsonify({"folder": _settings["currentFolder"], "settings": _settings})


@app.route("/api/pick-folder", methods=["POST"])
def pick_folder():
    """Abre un selector nativo de carpetas en Windows (pywebview o PowerShell fallback)."""
    selected_dir = None
    try:
        import webview
        if webview.windows:
            res = webview.windows[0].create_file_dialog(
                webview.FOLDER_DIALOG,
                directory=_settings.get("currentFolder", str(DEFAULT_BOOKS_DIR)),
            )
            if res and len(res) > 0:
                selected_dir = res[0]
    except Exception:
        pass

    if not selected_dir:
        try:
            ps_script = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "$f = New-Object System.Windows.Forms.FolderBrowserDialog; "
                "$f.Description = 'Selecciona la carpeta con tus libros PDF'; "
                # La ruta llega por una variable de entorno, nunca dentro del comando: una carpeta con
                # comillas (`O'Reilly`, o un nombre preparado) rompía el selector o ejecutaba código (SEC-087)
                "$f.SelectedPath = $env:PBM_CARPETA_INICIAL; "
                "if ($f.ShowDialog() -eq 'OK') { Write-Output $f.SelectedPath }"
            )
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-STA", "-Command", ps_script],
                capture_output=True,
                text=True,
                timeout=120,
                env={**os.environ, "PBM_CARPETA_INICIAL": str(_settings.get("currentFolder", str(DEFAULT_BOOKS_DIR)))},
            )
            out = (proc.stdout or "").strip()
            if out and Path(out).is_dir():
                selected_dir = out
        except Exception:
            pass

    if selected_dir and Path(selected_dir).is_dir():
        _settings["currentFolder"] = str(Path(selected_dir).resolve())
        _save_settings(_settings)
        return jsonify({"folder": _settings["currentFolder"], "cancelled": False})

    return jsonify({"folder": _settings["currentFolder"], "cancelled": True})


@app.route("/api/load", methods=["POST"])
def load_folder_fast():
    """
    Paso 1: Carga instantánea de todos los archivos PDF de la carpeta sin analizar su interior,
    para mostrar la lista completa en milisegundos.
    """
    global _books_list, _books_state
    data = request.get_json(silent=True) or {}
    _update_settings_from_payload(data)

    folder = Path(_settings["currentFolder"])
    if not folder.is_dir():
        folder = DEFAULT_BOOKS_DIR
        folder.mkdir(parents=True, exist_ok=True)
        _settings["currentFolder"] = str(folder.resolve())

    pdf_files = _list_pdf_files(folder, _settings.get("recursive", False))
    pattern = _settings.get("renamePattern", "{titulo} - {autor}")

    loaded: List[BookRecord] = []
    for pdf_path in pdf_files:
        rec = quick_load_pdf(filepath=pdf_path, base_dir=folder, rename_pattern=pattern)
        loaded.append(rec)

    _books_list = loaded
    _books_state = {b.id: b for b in loaded}
    return jsonify(_serialize_state())


@app.route("/api/analyze-one", methods=["POST"])
def analyze_one_book():
    """
    Paso 2: Analiza profundamente un libro individual (o finaliza detectando duplicados)
    para permitir barra de progreso en tiempo real en la interfaz.
    """
    data = request.get_json(silent=True) or {}
    book_id = data.get("bookId", "")
    is_last = bool(data.get("isLast", False))
    if "useOnline" in data:
        _settings["useOnline"] = bool(data["useOnline"])
    if "renamePattern" in data and data["renamePattern"]:
        _settings["renamePattern"] = str(data["renamePattern"]).strip()

    target = _books_state.get(book_id)
    if not target:
        return jsonify({"error": "Libro no encontrado"}), 404

    folder = Path(_settings["currentFolder"])
    analyzed_rec = analyze_single_pdf(
        filepath=Path(target.filepath),
        base_dir=folder,
        cache=_cache,
        use_online=_settings.get("useOnline", True),
        rename_pattern=_settings.get("renamePattern", "{titulo} - {autor}"),
    )

    # Reemplazar en _books_list manteniendo su posición
    for idx, b in enumerate(_books_list):
        if b.id == book_id:
            _books_list[idx] = analyzed_rec
            break
    _books_state[analyzed_rec.id] = analyzed_rec

    if is_last:
        _cache.save()
        detect_duplicates(_books_list)
        return jsonify(_serialize_state())

    return jsonify({
        "book": analyzed_rec.to_dict(),
        "stats": _compute_stats(_books_list),
    })


@app.route("/api/analyze-batch", methods=["POST"])
def analyze_batch_books():
    """
    Analiza un lote de libros en paralelo con múltiples hilos para máxima velocidad,
    actualizando la barra de progreso por lotes sin sobrecarga de red ni re-renderizado excesivo.
    """
    data = request.get_json(silent=True) or {}
    book_ids: List[str] = data.get("bookIds") or []
    is_last = bool(data.get("isLast", False))
    if "useOnline" in data:
        _settings["useOnline"] = bool(data["useOnline"])
    if "renamePattern" in data and data["renamePattern"]:
        _settings["renamePattern"] = str(data["renamePattern"]).strip()

    targets = [_books_state[bid] for bid in book_ids if bid in _books_state]
    if not targets:
        return jsonify({"error": "No se encontraron libros válidos en el lote"}), 404

    folder = Path(_settings["currentFolder"])
    use_online = _settings.get("useOnline", True)
    pattern = _settings.get("renamePattern", "{titulo} - {autor}")

    def _worker(t: BookRecord) -> BookRecord:
        return analyze_single_pdf(
            filepath=Path(t.filepath),
            base_dir=folder,
            cache=_cache,
            use_online=use_online,
            rename_pattern=pattern,
        )

    with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
        results = list(pool.map(_worker, targets))

    by_id = {r.id: r for r in results}
    for idx, b in enumerate(_books_list):
        if b.id in by_id:
            _books_list[idx] = by_id[b.id]
            _books_state[b.id] = by_id[b.id]

    if is_last:
        _cache.save()
        detect_duplicates(_books_list)
        return jsonify(_serialize_state())

    return jsonify({
        "batchBooks": [r.to_dict() for r in results],
        "stats": _compute_stats(_books_list),
    })


@app.route("/api/analyze-finish", methods=["POST"])
def analyze_finish():
    """Ejecuta la detección de duplicados sobre los libros analizados hasta el momento y guarda caché."""
    _cache.save()
    detect_duplicates(_books_list)
    return jsonify(_serialize_state())


@app.route("/api/clear", methods=["POST"])
def clear_workspace():
    """Limpia la lista actual de libros cargados/analizados y reinicia el estado en memoria."""
    global _books_list, _books_state
    data = request.get_json(silent=True) or {}
    _books_list = []
    _books_state = {}
    if data.get("clearCache"):
        _cache._data.clear()
        _cache.save()
    return jsonify(_serialize_state())


@app.route("/api/scan", methods=["POST"])
def scan_folder():
    """Endpoint completo de escaneo en un solo paso (usado para lotes pequeños o pruebas)."""
    global _books_list, _books_state
    data = request.get_json(silent=True) or {}
    _update_settings_from_payload(data)

    folder = Path(_settings["currentFolder"])
    if not folder.is_dir():
        folder = DEFAULT_BOOKS_DIR
        folder.mkdir(parents=True, exist_ok=True)
        _settings["currentFolder"] = str(folder.resolve())

    pdf_files = _list_pdf_files(folder, _settings.get("recursive", False))

    analyzed: List[BookRecord] = []
    for pdf_path in pdf_files:
        rec = analyze_single_pdf(
            filepath=pdf_path,
            base_dir=folder,
            cache=_cache,
            use_online=_settings.get("useOnline", True),
            rename_pattern=_settings.get("renamePattern", "{titulo} - {autor}"),
        )
        analyzed.append(rec)

    _cache.save()
    detect_duplicates(analyzed)

    _books_list = analyzed
    _books_state = {b.id: b for b in analyzed}

    return jsonify(_serialize_state())


@app.route("/api/books/<book_id>/update-draft", methods=["POST"])
def update_book_draft(book_id):
    b = _books_state.get(book_id)
    if not b:
        return jsonify({"error": "Libro no encontrado en la sesión actual"}), 404

    data = request.get_json(silent=True) or {}
    if "real_title" in data:
        b.real_title = str(data["real_title"]).strip()
    if "real_author" in data:
        b.real_author = str(data["real_author"]).strip()
    if "year" in data:
        b.year = str(data["year"]).strip()
    if "publisher" in data:
        b.publisher = str(data["publisher"]).strip()
    if "isbn" in data:
        b.isbn = str(data["isbn"]).strip()
    if "subject" in data:
        b.subject = str(data["subject"]).strip()
    if "language" in data:
        b.language = str(data["language"]).strip()
    if "description" in data:
        b.description = str(data["description"]).strip()

    orig_ext = Path(b.filename).suffix.lower() or ".pdf"
    if orig_ext not in (".pdf", ".epub"):
        orig_ext = ".pdf"

    if "suggested_filename" in data and data["suggested_filename"]:
        custom_fn = str(data["suggested_filename"]).strip()
        if not custom_fn.lower().endswith(orig_ext):
            custom_fn = Path(custom_fn).stem + orig_ext
        b.suggested_filename = custom_fn
    else:
        b.suggested_filename = build_suggested_filename(
            title=b.real_title,
            author=b.real_author,
            year=b.year,
            pattern=_settings.get("renamePattern", "{titulo} - {autor}"),
            fallback_stem=Path(b.filename).stem,
            ext=orig_ext,
        )

    b.needs_rename = b.filename != b.suggested_filename
    b.needs_metadata_update = (
        (b.real_title and b.current_title.strip() != b.real_title.strip())
        or (b.real_author and b.current_author.strip() != b.real_author.strip())
        or (b.subject and b.current_subject.strip() != b.subject.strip())
        or (b.isbn and b.isbn not in (b.current_keywords or ""))
    )

    return jsonify({
        "book": b.to_dict(),
        "stats": _compute_stats(_books_list),
    })


@app.route("/api/books/<book_id>/lookup", methods=["POST"])
def lookup_book_online(book_id):
    """Consulta en vivo Google Books y Open Library para un libro específico."""
    b = _books_state.get(book_id)
    if not b:
        return jsonify({"error": "Libro no encontrado"}), 404

    data = request.get_json(silent=True) or {}
    q_isbn = (data.get("isbn") if "isbn" in data else b.isbn).strip()
    q_title = (data.get("title") if "title" in data else b.real_title).strip()
    q_author = (data.get("author") if "author" in data else b.real_author).strip()

    info = query_google_books(isbn=q_isbn, title=q_title, author=q_author, timeout=5.0)
    if not info:
        info = query_open_library(isbn=q_isbn, title=q_title, author=q_author, timeout=5.0)

    if not info:
        return jsonify({"found": False, "message": "No se encontraron coincidencias en Google Books ni Open Library."})

    if info.get("title"):
        b.real_title = info["title"]
    if info.get("author"):
        b.real_author = info["author"]
    if info.get("year"):
        b.year = info["year"]
    if info.get("publisher"):
        b.publisher = info["publisher"]
    if info.get("isbn"):
        b.isbn = info["isbn"]
    if info.get("subject"):
        b.subject = info["subject"]
    if info.get("language"):
        b.language = info["language"]
    if info.get("description"):
        b.description = info["description"]
    b.source = info.get("source", "Google Books")
    b.confidence = info.get("confidence", "alta")
    b.analyzed = True

    b.suggested_filename = build_suggested_filename(
        title=b.real_title,
        author=b.real_author,
        year=b.year,
        pattern=_settings.get("renamePattern", "{titulo} - {autor}"),
        fallback_stem=Path(b.filename).stem,
        ext=Path(b.filename).suffix.lower() or ".pdf",
    )
    b.needs_rename = b.filename != b.suggested_filename
    b.needs_metadata_update = True

    return jsonify({
        "found": True,
        "book": b.to_dict(),
        "stats": _compute_stats(_books_list),
    })


@app.route("/api/pattern", methods=["POST"])
def update_rename_pattern():
    """Actualiza la plantilla de renombrado y recalcula todos los nombres sugeridos."""
    data = request.get_json(silent=True) or {}
    pattern = (data.get("renamePattern") or "{titulo} - {autor}").strip()
    _settings["renamePattern"] = pattern
    _save_settings(_settings)

    for b in _books_list:
        b.suggested_filename = build_suggested_filename(
            title=b.real_title,
            author=b.real_author,
            year=b.year,
            pattern=pattern,
            fallback_stem=Path(b.filename).stem,
            ext=Path(b.filename).suffix.lower() or ".pdf",
        )
        b.needs_rename = b.filename != b.suggested_filename

    return jsonify(_serialize_state())


@app.route("/api/duplicates/select-keeper", methods=["POST"])
def select_duplicate_keeper():
    """Cambia cuál archivo se conserva dentro de un grupo de duplicados."""
    data = request.get_json(silent=True) or {}
    book_id = data.get("bookId", "")
    target = _books_state.get(book_id)
    if not target or not target.duplicate_group_id:
        return jsonify({"error": "Libro o grupo de duplicados no encontrado"}), 404

    gid = target.duplicate_group_id
    group_members = [b for b in _books_list if b.duplicate_group_id == gid]
    for m in group_members:
        if m.id == target.id:
            m.keep_in_group = True
            m.is_duplicate = False
            m.duplicate_of = ""
        else:
            m.keep_in_group = False
            m.is_duplicate = True
            m.duplicate_of = target.filename

    return jsonify(_serialize_state())


@app.route("/api/duplicates/remove", methods=["POST"])
def remove_duplicates_endpoint():
    """Elimina o mueve los duplicados seleccionados (o todos los detectados)."""
    global _books_list, _books_state
    data = request.get_json(silent=True) or {}
    book_ids = data.get("bookIds")
    mode = data.get("mode") or _settings.get("duplicateMode", "trash")
    if mode in ("trash", "folder", "delete"):
        _settings["duplicateMode"] = mode
        _save_settings(_settings)

    if book_ids is not None:
        to_remove = [b for b in _books_list if b.id in set(book_ids)]
    else:
        to_remove = [b for b in _books_list if b.is_duplicate]

    base_dir = Path(_settings["currentFolder"])
    removed_count = 0
    freed_bytes = 0
    errors = []
    removed_ids = set()

    for b in to_remove:
        ok, msg = remove_duplicate_file(Path(b.filepath), base_dir, mode=mode)
        if ok:
            removed_count += 1
            freed_bytes += b.size_bytes
            removed_ids.add(b.id)
        else:
            errors.append({"filename": b.filename, "error": msg})

    _books_list = [b for b in _books_list if b.id not in removed_ids]
    detect_duplicates(_books_list)
    _books_state = {b.id: b for b in _books_list}

    res = _serialize_state()
    res["removedCount"] = removed_count
    res["freedHuman"] = format_size(freed_bytes)
    res["errors"] = errors
    return jsonify(res)


@app.route("/api/books/apply", methods=["POST"])
def apply_changes_endpoint():
    """
    Escribe los metadatos reales en el PDF y renombra el archivo en disco
    para los libros indicados (o todos los libros principales no duplicados).
    """
    global _books_list, _books_state
    data = request.get_json(silent=True) or {}
    book_ids = data.get("bookIds")
    write_meta = data.get("writeMetadata", True)
    do_rename = data.get("doRename", True)
    is_last = bool(data.get("isLast", True))

    if book_ids is not None:
        targets = [b for b in _books_list if b.id in set(book_ids)]
    else:
        targets = [
            b for b in _books_list
            if not b.is_duplicate and (b.needs_rename or b.needs_metadata_update)
        ]

    base_dir = Path(_settings["currentFolder"])
    updated_count = 0
    errors = []

    def _apply_worker(b: BookRecord):
        ok, msg, new_path = write_pdf_metadata_and_rename(
            filepath=Path(b.filepath),
            new_filename=b.suggested_filename,
            title=b.real_title,
            author=b.real_author,
            subject=b.subject,
            isbn=b.isbn,
            publisher=b.publisher,
            year=b.year,
            language=b.language,
            write_metadata=write_meta,
            do_rename=do_rename,
        )
        return b, ok, msg, new_path

    if len(targets) > 1:
        with ThreadPoolExecutor(max_workers=min(6, len(targets))) as pool:
            worker_results = list(pool.map(_apply_worker, targets))
    else:
        worker_results = [_apply_worker(b) for b in targets]

    for b, ok, msg, new_path in worker_results:
        if ok:
            updated_count += 1
            b.filepath = str(new_path)
            b.filename = new_path.name
            try:
                b.rel_path = str(new_path.relative_to(base_dir))
            except ValueError:
                b.rel_path = new_path.name
            if new_path.is_file():
                b.size_bytes = new_path.stat().st_size
                b.size_human = format_size(b.size_bytes)
            if write_meta:
                b.current_title = b.real_title
                b.current_author = b.real_author
                b.current_subject = b.subject
                kw_parts = []
                if b.isbn:
                    kw_parts.append(f"ISBN: {b.isbn}")
                if b.publisher:
                    kw_parts.append(f"Editorial: {b.publisher}")
                if b.year:
                    kw_parts.append(f"Año: {b.year}")
                b.current_keywords = " | ".join(kw_parts)
                b.needs_metadata_update = False
            if do_rename:
                b.suggested_filename = b.filename
                b.needs_rename = False
        else:
            errors.append({"filename": b.filename, "error": msg})

    if is_last:
        detect_duplicates(_books_list)
        _books_state = {b.id: b for b in _books_list}
        res = _serialize_state()
        res["updatedCount"] = updated_count
        res["errors"] = errors
        return jsonify(res)

    return jsonify({
        "batchBooks": [b.to_dict() for b in targets],
        "stats": _compute_stats(_books_list),
        "updatedCount": updated_count,
        "errors": errors,
    })


@app.route("/api/books/<book_id>/pdf")
def serve_pdf(book_id):
    """Sirve el archivo PDF o una vista lectora HTML para EPUB validando que pertenezca a la carpeta escaneada."""
    b = _books_state.get(book_id)
    if not b:
        return jsonify({"error": "Libro no encontrado"}), 404
    folder = Path(_settings["currentFolder"]).resolve()
    file_path = Path(b.filepath).resolve()
    try:
        if not file_path.is_relative_to(folder):
            return jsonify({"error": "Acceso no permitido fuera de la carpeta activa"}), 403
    except ValueError:
        return jsonify({"error": "Ruta inválida"}), 400
    if not file_path.is_file():
        return jsonify({"error": "Archivo no encontrado en disco"}), 404
    if file_path.suffix.lower() == ".epub":
        import zipfile
        import re as _re
        chapters_html = []
        try:
            with zipfile.ZipFile(file_path, "r") as zf:
                html_files = [
                    n for n in zf.namelist()
                    if n.lower().endswith((".xhtml", ".html", ".htm"))
                ]
                for hf in html_files[:12]:
                    raw = zf.read(hf).decode("utf-8", errors="ignore")
                    body_m = _re.search(r"<body[^>]*>(.*?)</body>", raw, flags=_re.DOTALL | _re.IGNORECASE)
                    chunk = body_m.group(1) if body_m else raw
                    chunk = _re.sub(r"<img[^>]*>", "", chunk, flags=_re.IGNORECASE)
                    chunk = _re.sub(r"<svg[^>]*>.*?</svg>", "", chunk, flags=_re.DOTALL | _re.IGNORECASE)
                    # <meta http-equiv="refresh"> llevaría la vista previa a otra página
                    chunk = _re.sub(r"<meta\b[^>]*>", "", chunk, flags=_re.IGNORECASE)
                    if chunk.strip():
                        chapters_html.append(f"<section class='chap'>{chunk}</section>")
        except Exception as e:
            chapters_html.append(f"<p>Error al abrir EPUB: {html.escape(str(e))}</p>")
        joined = "\n<hr>\n".join(chapters_html) if chapters_html else "<p>Sin vista previa de texto.</p>"
        doc = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
        body {{ font-family: Georgia, serif; max-width: 720px; margin: 24px auto; padding: 0 20px; line-height: 1.65; color: #1e293b; background: #f8fafc; }}
        h1, h2, h3 {{ font-family: system-ui, sans-serif; color: #0f172a; }}
        hr {{ border: none; border-top: 1px solid #cbd5e1; margin: 28px 0; }}
        .hdr {{ background: #e2e8f0; padding: 12px 16px; border-radius: 8px; margin-bottom: 20px; font-family: system-ui, sans-serif; font-size: 13px; }}
        </style></head><body>
        <div class="hdr"><strong>EPUB:</strong> {html.escape(b.real_title or b.filename)} — <em>{html.escape(b.real_author or '')}</em></div>
        {joined}
        </body></html>"""
        # El HTML del libro viene de cualquier parte: un EPUB descargado puede traer <script>. Con `sandbox` la
        # vista previa queda en un origen propio, sin scripts ni formularios, y no puede llamar a esta API
        # (borrar o renombrar libros). `default-src 'none'` impide cargar nada de fuera (SEC-085).
        return doc, 200, {
            "Content-Type": "text/html; charset=utf-8",
            "Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:",
            "X-Content-Type-Options": "nosniff",
        }
    return send_file(file_path, mimetype="application/pdf")
