from pathlib import Path
from pypdf import PdfReader, PdfWriter

from pdf_engine import (
    MetadataCache,
    analyze_single_pdf,
    build_suggested_filename,
    clean_filename_and_parse,
    detect_duplicates,
    extract_isbns,
    is_garbage_meta,
    validate_isbn,
    write_pdf_metadata_and_rename,
)


def test_validate_and_extract_isbn():
    assert validate_isbn("978-84-376-0494-7") == "9788437604947"
    assert validate_isbn("0-306-40615-2") == "0306406152"
    assert validate_isbn("9788437604940") is None  # Dígito de control inválido

    txt = "Publicado por Editorial Sudamericana. ISBN: 978-84-376-0494-7. Impreso en España."
    assert extract_isbns(txt) == ["9788437604947"]


def test_clean_filename_and_parse():
    r1 = clean_filename_and_parse("109283_Cien_anos_de_soledad_-_Gabriel_Garcia_Marquez_(z-lib.org) (1).pdf")
    assert "Cien" in r1["title"]
    assert "Gabriel" in r1["author"]

    r2 = clean_filename_and_parse("Robert C. Martin - Clean Code (2008) [pdf].pdf")
    assert r2["author"] == "Robert C. Martin"
    assert r2["title"] == "Clean Code"
    assert r2["year"] == "2008"


def test_garbage_metadata():
    assert is_garbage_meta("Microsoft Word - Documento1.docx") is True
    assert is_garbage_meta("untitled") is True
    assert is_garbage_meta("admin") is True
    assert is_garbage_meta("lector.ejemplo@example.com") is True
    assert is_garbage_meta("El amor en los tiempos del cólera") is False


def test_email_and_username_author_fallback_to_filename(tmp_path: Path):
    cache = MetadataCache(tmp_path / "cache.json")

    # Caso 1: PDF tiene correo electrónico en /Author pero el nombre de archivo tiene "Joana Marcus"
    pdf_a = tmp_path / "(Extraños II) Sempiterno - Joana Marcus.pdf"
    w1 = PdfWriter()
    w1.add_blank_page(width=400, height=600)
    w1.add_metadata({"/Author": "lector.ejemplo@example.com"})
    with open(pdf_a, "wb") as f:
        w1.write(f)

    rec_a = analyze_single_pdf(pdf_a, tmp_path, cache, use_online=False)
    assert rec_a.real_title == "(Extraños II) Sempiterno"
    assert rec_a.real_author == "Joana Marcus"

    # Caso 2: PDF tiene usuario corto "Acb" en /Author y decimal "0.5 - " al inicio del archivo
    pdf_b = tmp_path / "0.5 - Seal Of Her Dreams - Paige Tyler.pdf"
    w2 = PdfWriter()
    w2.add_blank_page(width=400, height=600)
    w2.add_metadata({"/Author": "Acb"})
    with open(pdf_b, "wb") as f:
        w2.write(f)

    rec_b = analyze_single_pdf(pdf_b, tmp_path, cache, use_online=False)
    assert rec_b.real_title == "Seal Of Her Dreams"
    assert rec_b.real_author == "Paige Tyler"
    assert rec_b.suggested_filename == "Seal Of Her Dreams - Paige Tyler.pdf"


def test_pdf_metadata_write_rename_and_duplicates(tmp_path: Path):
    cache = MetadataCache(tmp_path / "cache.json")

    # Crear un PDF de prueba
    pdf1 = tmp_path / "123456_El_Principito_-_Antoine_de_Saint-Exupery_(z-lib.org).pdf"
    w = PdfWriter()
    w.add_blank_page(width=400, height=600)
    w.add_metadata({"/Title": "Microsoft Word - scan.doc", "/Author": "admin"})
    with open(pdf1, "wb") as f:
        w.write(f)

    # Crear una copia exacta con otro nombre
    pdf1_dup = tmp_path / "copia_principito (1).pdf"
    pdf1_dup.write_bytes(pdf1.read_bytes())

    b1 = analyze_single_pdf(pdf1, tmp_path, cache, use_online=False)
    b2 = analyze_single_pdf(pdf1_dup, tmp_path, cache, use_online=False)

    books = [b1, b2]
    detect_duplicates(books)

    assert any(b.is_duplicate for b in books)
    assert any(b.keep_in_group and not b.is_duplicate for b in books)

    # Probar escritura de metadatos y renombrado sobre b1
    suggested = build_suggested_filename("El Principito", "Antoine de Saint-Exupéry")
    ok, msg, new_path = write_pdf_metadata_and_rename(
        filepath=pdf1,
        new_filename=suggested,
        title="El Principito",
        author="Antoine de Saint-Exupéry",
        subject="Fábula, Literatura infantil",
        isbn="9788498381498",
        publisher="Salamandra",
        year="1943",
        write_metadata=True,
        do_rename=True,
    )
    assert ok is True, msg
    assert new_path.name == "El Principito - Antoine de Saint-Exupéry.pdf"
    assert new_path.exists()

    reader = PdfReader(str(new_path))
    assert reader.metadata.get("/Title") == "El Principito"
    assert reader.metadata.get("/Author") == "Antoine de Saint-Exupéry"
    assert "9788498381498" in (reader.metadata.get("/Keywords") or "")


def test_sagas_and_continuations_are_never_marked_as_duplicates(tmp_path: Path):
    from pdf_engine import quick_load_pdf

    saga_filenames = [
        # Saga Cat and Mouse Duet (títulos muy parecidos: Haunting vs Hunting)
        "1- Haunting Adeline - H. D. Carlton.pdf",
        "02. Hunting Adeline - H. D. Carlton.pdf",
        # Saga con numeración romana al final
        "4. Suelo sagrado I - Taylor Jenkins Reid.pdf",
        "5. Suelo sagrado II - Taylor Jenkins Reid.pdf",
        # Saga con Vol 1 / Vol 2
        "6. Yo soy Eric Zimmerman. Vol 1 - Megan Maxwell.pdf",
        "7. Yo soy Eric Zimmerman. Vol 2 - Megan Maxwell.pdf",
        # Saga separada por guiones bajos que difiere en una sola palabra ("tu" vs "yo")
        "Alguien_como_tu_Elisabet_Benavent.pdf",
        "Alguien_como_yo_Elisabet_Benavent.pdf",
        # Saga con el mismo título base pero numeración inicial distinta ("1." vs "2.")
        "1. Perfectos mentirosos - Alex Mírez(1).pdf",
        "2. Perfectos mentirosos - Alex Mírez(1).pdf",
        # Saga con prefijo _Serie_ largo
        "01_Ruby_Dixon_Serie_Ice_Planet_Barbarian_Ice_Planet_Barbarian.pdf",
        "04_Ruby_Dixon_Serie_Ice_Planet_Barbarian_Ice_Planet_Barbarian.pdf",
        "06_Ruby_Dixon_Serie_Ice_Planet_Barbarian_Barbarian_s_Mate.pdf",
        "09_Ruby_Dixon_Serie_Ice_Planet_Barbarian_Barbarian_s_Heart.pdf",
    ]

    for name in saga_filenames:
        p = tmp_path / name
        w = PdfWriter()
        w.add_blank_page(width=400, height=600)
        with open(p, "wb") as f:
            w.write(f)

    books = [quick_load_pdf(tmp_path / name, tmp_path) for name in saga_filenames]
    # Simular incluso que varios libros comparten la misma página inicial de créditos del foro/traductor
    shared_intro = "Esta traduccion fue hecha sin fines de lucro de lectores para lectores " * 25
    for b in books:
        b.text_fingerprint = shared_intro

    detect_duplicates(books)

    assert all(not b.is_duplicate for b in books), f"Falso positivo en sagas: {[b.filename for b in books if b.is_duplicate]}"
    assert all(b.duplicate_group_id == "" for b in books)

    # Verificar que Perfectos mentirosos 1 y 2 conservaron su número de tomo en el nombre sugerido
    pm_books = [b for b in books if "Perfectos mentirosos" in b.filename]
    assert pm_books[0].suggested_filename == "Perfectos mentirosos 1 - Alex Mírez.pdf"
    assert pm_books[1].suggested_filename == "Perfectos mentirosos 2 - Alex Mírez.pdf"


def test_tricky_filenames_and_epub_support(tmp_path: Path):
    import zipfile
    from pdf_engine import read_epub_metadata

    cases = [
        ("Grace H Desastre - 1 Un Desastre Llamado Valentina(1).pdf", "Un Desastre Llamado Valentina", "Grace H Desastre"),
        ("1 Un Desastre Llamado Valentina - Grace H Desastre.pdf", "Un Desastre Llamado Valentina", "Grace H Desastre"),
        ("02 - Neva Altaj - Broken Whispers.epub", "Broken Whispers", "Neva Altaj"),
        ("03.5 Day Zero - Kresley Cole.epub", "Day Zero", "Kresley Cole"),
        ("04 - Neva Altaj - Ruined Secrets.epub", "Ruined Secrets", "Neva Altaj"),
        ("2. P.D. Todavía te quiero - Jenny Han(1).pdf", "P.D. Todavía te quiero", "Jenny Han"),
        ("habitos-atomicos-james-clear-(1).pdf", "Habitos Atomicos", "James Clear"),
        ("Persuasion-Jane_Austen(1).pdf", "Persuasion", "Jane Austen"),
    ]
    for fname, exp_title, exp_author in cases:
        parsed = clean_filename_and_parse(fname)
        assert parsed["title"] == exp_title, f"Falló título en {fname}: {parsed}"
        assert parsed["author"] == exp_author, f"Falló autor en {fname}: {parsed}"

    # Crear un EPUB con nombre basura 'hdhfhf.epub' pero metadatos internos válidos
    epub_path = tmp_path / "hdhfhf.epub"
    container_xml = """<?xml version="1.0"?>
    <container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
      <rootfiles>
        <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
      </rootfiles>
    </container>"""
    opf_xml = """<?xml version="1.0" encoding="utf-8"?>
    <package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0">
      <metadata>
        <dc:title>Hooked</dc:title>
        <dc:creator>Emily McIntire</dc:creator>
        <dc:publisher>Bloom Books</dc:publisher>
        <dc:date>2021-09-01</dc:date>
        <dc:language>en</dc:language>
      </metadata>
    </package>"""
    with zipfile.ZipFile(epub_path, "w") as zf:
        zf.writestr("META-INF/container.xml", container_xml)
        zf.writestr("OEBPS/content.opf", opf_xml)
        zf.writestr("OEBPS/chap1.xhtml", "<html><body><h1>Chapter 1</h1><p>Hello world</p></body></html>")

    cache = MetadataCache(tmp_path / "cache.json")
    rec = analyze_single_pdf(epub_path, tmp_path, cache, use_online=False)
    assert rec.file_format == "EPUB"
    assert rec.real_title == "Hooked"
    assert rec.real_author == "Emily McIntire"
    assert rec.suggested_filename == "Hooked - Emily McIntire.epub"

    ok, msg, new_epub = write_pdf_metadata_and_rename(
        filepath=epub_path,
        new_filename=rec.suggested_filename,
        title="Hooked",
        author="Emily McIntire",
        subject="Dark Romance",
        publisher="Bloom Books",
        year="2021",
        write_metadata=True,
        do_rename=True,
    )
    assert ok is True, msg
    assert new_epub.name == "Hooked - Emily McIntire.epub"
    meta_after = read_epub_metadata(new_epub)
    assert meta_after["title"] == "Hooked"
    assert meta_after["author"] == "Emily McIntire"



