"""
Motor de análisis de libros PDF:
- Escaneo de carpetas y cálculo de hash SHA-256
- Extracción local de metadatos, texto de portada, ISBN, editorial y año
- Consulta híbrida a Google Books API y Open Library API con caché local
- Detección de duplicados exactos (SHA-256) y por similitud (ISBN / Título+Autor / Contenido)
- Escritura de metadatos PDF reales y renombrado seguro de archivos
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import urllib.parse

import requests
from pypdf import PdfReader, PdfWriter

# Identifica la app ante Google Books y Open Library (piden un User-Agent propio), sin datos personales.
USER_AGENT = "PDFBookManager/1.1.1 (DMDev Labs; +https://dmdevlabs.com)"


# ─────────────────────────────────────────────────────────────────────────────
# Constantes y Expresiones Regulares
# ─────────────────────────────────────────────────────────────────────────────

_NOISE_PATTERNS = [
    r"\(z-lib\.org\)",
    r"\(z-library\)",
    r"z-lib\.org",
    r"z-library",
    r"annas-archive\.[a-z]+",
    r"annas-archive",
    r"libgen\.li",
    r"libgen\.is",
    r"libgen\.rs",
    r"libgen",
    r"dokumen\.pub[_\-\s]*",
    r"vdocuments\.[a-z]+[_\-\s]*",
    r"pdfdrive\.com",
    r"pdfdrive",
    r"lectulandia",
    r"epublibre",
    r"espapdf",
    r"holaebook",
    r"ww2\.ebookelo\.[a-z]+",
    r"ebookelo",
    r"www\.[a-z0-9\-]+\.[a-z]{2,4}",
    r"\[(?:pdf|epub|mobi|azw3|español|spanish|english|castellano|ocr|scan|completo)\]",
    r"\((?:pdf|epub|mobi|español|spanish|english|castellano|ocr|scan)\)",
    r"\b(?:pdf|epub|mobi)\b$",
]

_GARBAGE_META_PATTERNS = [
    r"^microsoft word",
    r"^untitled",
    r"^sin t[ií]tulo",
    r"^documento\d*",
    r"^document\d*",
    r"^slide\s*\d*",
    r"^presentaci[oó]n",
    r"^user$",
    r"^usuario$",
    r"^admin$",
    r"^administrador$",
    r"^administrator$",
    r"^anon(?:ymous)?$",
    r"^desconocido$",
    r"^unknown$",
    r"^none$",
    r"^null$",
    r"^n/?a$",
    r"^calibre",
    r"^quarkxpress",
    r"^indesign",
    r"^acrobat",
    r"^scanner",
    r"^scan$",
    r"\.(?:doc|docx|pdf|indd|qxp|txt|epub|odt|pmd)$",
    r"^[0-9_\-\s\.]+$",
]

_ISBN_REGEX = re.compile(
    r"(?:ISBN(?:-1[03])?[:\s\-]*)?((?:97[89][\s\-]?)?(?:\d[\s\-]?){9}[\dXx])\b",
    re.IGNORECASE,
)

_YEAR_REGEX = re.compile(r"\b(19[2-9]\d|20[0-2]\d)\b")

_COPYRIGHT_YEAR_REGEX = re.compile(
    r"(?:©|copyright|edici[oó]n|published|publicado|impreso)[^\n\d]{0,30}\b(19\d\d|20[0-2]\d)\b",
    re.IGNORECASE,
)

_PUBLISHER_REGEX = re.compile(
    r"(?:editorial|ediciones|publicado por|published by|publisher|grupo editorial|sello)[:\s]+([A-ZÁÉÍÓÚÑ][A-Za-záéíóúñÁÉÍÓÚÑ0-9\s,\.&\-]{2,45})",
    re.IGNORECASE,
)

_KNOWN_PUBLISHERS = [
    "Alfaguara", "Planeta", "Anagrama", "Tusquets", "Debolsillo", "Penguin Random House",
    "Random House", "Grijalbo", "Seix Barral", "Fondo de Cultura Económica", "Siglo XXI",
    "Alianza Editorial", "Cátedra", "Espasa", "Paidós", "Taurus", "Ariel", "Crítica",
    "Salamandra", "Siruela", "Acantilado", "Páginas de Espuma", "Impedimenta", "Sexto Piso",
    "O'Reilly", "Packt", "Manning", "No Starch Press", "Apress", "Addison-Wesley",
    "Pearson", "McGraw-Hill", "Wiley", "Springer", "MIT Press", "Cambridge University Press",
    "Oxford University Press", "HarperCollins", "Simon & Schuster", "Macmillan", "Hachette",
    "RBA", "Ediciones B", "Plaza & Janés", "Suma de Letras", "Aguilar", "Urano", "Kairós",
]

_INVALID_WIN_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# ─────────────────────────────────────────────────────────────────────────────
# Funciones Auxiliares de Limpieza y Validación
# ─────────────────────────────────────────────────────────────────────────────

def normalize_for_compare(text: str) -> str:
    """Normaliza texto en minúsculas sin acentos ni signos para comparación."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def format_size(size_bytes: int) -> str:
    """Convierte bytes a formato legible (KB, MB)."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    kb = size_bytes / 1024
    if kb < 1024:
        return f"{kb:.1f} KB"
    mb = kb / 1024
    return f"{mb:.2f} MB"


def is_garbage_meta(val: Optional[str], filename_stem: str = "") -> bool:
    """Determina si un campo de metadatos PDF es basura o irrelevante."""
    if not val:
        return True
    s = val.strip()
    if len(s) < 2 or len(s) > 220:
        return True
    # Correos electrónicos o URLs nunca son títulos ni autores válidos
    if "@" in s or "http://" in s.lower() or "https://" in s.lower() or "www." in s.lower():
        return True
    if re.search(r"\.(?:com|org|net|es|io|edu|gov)\b", s, re.IGNORECASE):
        return True
    for pat in _GARBAGE_META_PATTERNS:
        if re.search(pat, s, re.IGNORECASE):
            return True
    # Si es exactamente igual al nombre de archivo lleno de guiones bajos o hashes
    if "_" in s and s.count("_") >= 2:
        return True
    if re.match(r"^[0-9a-fA-F]{16,}$", s):
        return True
    return False


def is_valid_author_meta(val: Optional[str]) -> bool:
    """
    Verifica estrictamente si un metadato /Author del PDF es realmente el nombre de un autor
    y no un correo (ej. lector.ejemplo@example.com) ni un usuario de PC (ej. Acb, Lenovo, User1).
    """
    if is_garbage_meta(val):
        return False
    s = (val or "").strip()
    # Un nombre de autor real no contiene números ni arrobas ni guiones bajos
    if any(c.isdigit() for c in s) or "@" in s or "_" in s:
        return False
    words = [w for w in s.split() if w]
    # Evitar palabras sueltas de cuenta de usuario en Windows/Word como "Acb", "Asus", "Maria", "Wattpad"
    if len(words) < 2 or len(s) < 5:
        return False
    return looks_like_person_name(s)


def validate_isbn(raw_isbn: str) -> Optional[str]:
    """Limpia y valida el dígito de control de un ISBN-10 o ISBN-13."""
    digits = re.sub(r"[^0-9Xx]", "", raw_isbn).upper()
    if len(digits) == 10:
        # Evitar falsos positivos como 0000000000 o números consecutivos simples
        if len(set(digits)) <= 2:
            return None
        total = 0
        for i in range(9):
            if not digits[i].isdigit():
                return None
            total += (10 - i) * int(digits[i])
        check = 10 if digits[9] == "X" else (int(digits[9]) if digits[9].isdigit() else -1)
        if check < 0:
            return None
        total += check
        if total % 11 == 0:
            return digits
    elif len(digits) == 13 and digits.isdigit() and digits.startswith(("978", "979")):
        total = 0
        for i in range(12):
            factor = 1 if i % 2 == 0 else 3
            total += int(digits[i]) * factor
        check = (10 - (total % 10)) % 10
        if check == int(digits[12]):
            return digits
    return None


def extract_isbns(text: str) -> List[str]:
    """
    Extrae los ISBN válidos presentes en un texto, ignorando líneas que hagan referencia
    a 'obra completa', 'colección' o listas de otros libros de la saga.
    """
    if not text:
        return []
    found = []
    # Filtrar líneas que hablen de obra completa, colección o catálogo de otros tomos
    clean_lines = []
    for line in text.splitlines():
        if re.search(r"obra completa|colecci[oó]n|otros t[ií]tulos|tambi[eé]n de|box\s*set", line, re.IGNORECASE):
            continue
        clean_lines.append(line)
    filtered_text = "\n".join(clean_lines)

    explicit_matches = re.finditer(
        r"ISBN(?:-1[03])?[:\s\-]*((?:97[89][\s\-]?)?(?:\d[\s\-]?){9}[\dXx])\b",
        filtered_text,
        re.IGNORECASE,
    )
    for m in explicit_matches:
        valid = validate_isbn(m.group(1))
        if valid and valid not in found:
            found.append(valid)

    for m in _ISBN_REGEX.finditer(filtered_text):
        candidate = m.group(1)
        valid = validate_isbn(candidate)
        if valid and (len(valid) == 13 or "isbn" in m.group(0).lower()):
            if valid not in found:
                found.append(valid)

    # Si en las primeras páginas aparecen 3 o más ISBNs distintos (ej. página con toda la saga),
    # no devolver ninguno para evitar asignar el ISBN del Tomo 1 a los demás tomos.
    if len(found) >= 3:
        return []
    return found


def smart_title_case(text: str) -> str:
    """Aplica capitalización limpia si el texto está todo en mayúsculas o todo en minúsculas, preservando números romanos."""
    if not text:
        return ""
    s = re.sub(r"\s+", " ", text).strip()
    roman_set = {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii", "xiii", "xiv", "xv"}
    if s.isupper() or s.islower():
        small_words = {
            "de", "del", "la", "las", "el", "los", "y", "e", "o", "u", "en", "a",
            "con", "por", "para", "sin", "sobre", "entre", "of", "the", "and", "in",
            "to", "for", "with", "on", "at", "from", "by",
        }
        words = s.split(" ")
        result = []
        for idx, w in enumerate(words):
            lw = w.lower()
            clean_w = lw.strip("().,;:-")
            if clean_w in roman_set and (idx > 0 or len(words) == 1):
                result.append(w.upper())
            elif idx > 0 and lw in small_words:
                result.append(lw)
            else:
                result.append(lw.capitalize())
        return " ".join(result)
    return s


def looks_like_person_name(text: str) -> bool:
    """Heurística para detectar si un segmento parece el nombre de un autor."""
    if not text:
        return False
    s = text.strip()
    if any(c.isdigit() for c in s) or "@" in s:
        return False
    # Excluir "P.D." o "P.S." (Posdata / Postscript en títulos como "P.D. Todavía te quiero")
    if re.search(r"\bP\.?\s*[DS]\.", s, re.IGNORECASE):
        return False
    words = s.split()
    if not (2 <= len(words) <= 4):
        return False
    # Si contiene palabras típicas de títulos de libros, preposiciones o pronombres, no es autor
    title_words = {
        "introduccion", "manual", "guia", "historia", "teoria", "curso", "libro",
        "tratado", "diccionario", "enciclopedia", "arte", "ciencia", "secretos",
        "fundamentos", "principios", "como", "cuando", "donde", "porque",
        "introduction", "handbook", "guide", "history", "theory", "book", "edition",
        "edicion", "volumen", "tomo", "parte", "capitulo", "apuntes", "resumen",
        "el", "la", "los", "las", "un", "una", "unos", "unas", "the", "how",
        "clean", "code", "programming", "python", "java", "javascript", "design",
        "patterns", "system", "systems", "data", "learning", "machine", "network",
        "security", "practical", "modern", "effective", "mastering", "advanced",
        "algorithms", "architecture", "software", "database", "analysis",
        "habitos", "atomicos", "poder", "mente", "vida", "amor", "tiempo",
        "soledad", "anos", "mundo", "guerra", "paz", "psicologia", "filosofia",
        "economia", "finanzas", "negocios",
        # Preposiciones, pronombres y palabras frecuentes en títulos de novelas (EN/ES)
        "for", "her", "his", "my", "your", "our", "their", "me", "you", "him", "us", "them",
        "of", "to", "in", "on", "at", "with", "from", "by", "into", "about", "after", "before",
        "under", "over", "between", "without", "within", "against", "and", "or",
        "seal", "strong", "silent", "texas", "undercover", "shatter", "unravel", "ignite",
        "restore", "defy", "imagine", "captive", "prince", "king", "kings", "queen",
        "princess", "duke", "lord", "lady", "dark", "light", "shadow", "shadows", "blood",
        "bone", "bones", "ash", "fire", "ice", "crown", "throne", "court", "wings", "ruin",
        "ruined", "secrets", "fractured", "arcana", "rising", "calling", "zero", "painted",
        "hidden", "merciless", "heartless", "ruthless", "savage", "syndicates", "priest",
        "saints", "cruel", "tears", "betrayal", "salvation", "dare", "vicious", "darling",
        "perfectly", "imperfect", "unbreakable", "undeniable", "throttled", "purity", "hooked",
        "mist", "fury", "star", "stars", "night", "day", "days", "heart", "hearts", "soul",
        "souls", "sin", "sinner", "sinners", "sweet", "little", "broken", "stolen", "taken",
        "owned", "chosen", "captured", "twisted", "hate", "lies", "games", "love", "kiss",
        "touch", "touches", "scars", "whispers", "truths", "dreams", "dream", "warrior",
        "barbarian", "barbarians", "alien", "aliens", "planet", "holiday", "honeymoon", "shocks",
        "para", "su", "sus", "mi", "mis", "tu", "tus", "por", "con", "sin", "sobre",
        "entre", "hacia", "hasta", "desde", "contra", "antes", "despues", "corazon",
        "reina", "rey", "principe", "princesa", "cielo", "infierno", "sombra", "sombras",
        "sangre", "fuego", "hielo", "corona", "trono", "corte", "alas", "noche", "dia",
        "dias", "alma", "almas", "pecado", "beso", "secreto", "peligro", "pasion", "dolor",
        "redencion", "sacrificio", "salvacion", "incomprendidos", "club", "latidos", "dije",
        "destruye", "sanar", "instructivo", "irse", "tuya", "quieras", "limites", "brujas",
        "dioses", "sol", "ladrona", "luna", "queda", "nosotros", "boss", "bratva", "puedo",
        "sonar", "contigo", "todavia", "quiero", "amigos", "oscura", "orgullo", "prejuicio",
        "tren", "chica", "chico", "ventana", "cancion", "lobo", "cuervo", "cenizas", "torre",
        "alba", "imperio", "tormentas", "heredera", "medianoche", "maravillosa", "maravilloso",
        "ganas", "escala", "cuento", "perfecto", "llamado",
    }
    norm_words = [normalize_for_compare(w) for w in words]
    if any(w in title_words for w in norm_words):
        return False
    # Si tiene iniciales tipo "J. K. Rowling", "R.G. Angel" o "H. D. Carlton"
    if any(re.match(r"^[A-Z]\.(?:[A-Z]\.)?$", w) for w in words):
        return True
    # Si son 2 o 3 palabras que empiezan con mayúscula (o conectores de apellidos como "de", "del", "van")
    connectors = {"de", "del", "la", "las", "los", "van", "von", "da", "di", "st", "st."}
    if words[0].lower() in connectors or words[-1].lower() in connectors:
        return False
    if 2 <= len(words) <= 3 and all((w[0].isupper() or w.lower() in connectors) for w in words if w):
        return True
    if len(words) == 4 and any(w.lower() in connectors for w in words):
        return True
    return False


def is_gibberish_title(text: str) -> bool:
    """Detecta si un título corto extraído del nombre de archivo es texto basura sin vocales (ej. 'hdhfhf')."""
    if not text:
        return True
    s = re.sub(r"[^a-zA-ZáéíóúñÁÉÍÓÚÑ]", "", text.strip())
    if len(s) < 2:
        return True
    if not re.search(r"[aeiouáéíóúyAEIOUÁÉÍÓÚY]", s):
        return True
    return False


def fix_broken_accents(text: str) -> str:
    """Corrige mojibake UTF-8 (ej. soÃ±ar -> soñar), tildes NFD descompuestas, n~ -> ñ y vocales con tilde suelta."""
    if not text:
        return ""
    s = text
    if "Ã" in s or "Â" in s:
        try:
            s = s.encode("latin-1").decode("utf-8")
        except Exception:
            pass
    s = unicodedata.normalize("NFC", s)
    s = s.replace("n~", "ñ").replace("N~", "Ñ")
    accent_map = {"a": "á", "e": "é", "i": "í", "o": "ó", "u": "ú", "A": "Á", "E": "É", "I": "Í", "O": "Ó", "U": "Ú"}
    for vowel, accented in accent_map.items():
        s = s.replace(vowel + "´", accented).replace(vowel + "\u0301", accented)
    return unicodedata.normalize("NFC", s)


def _strip_leading_series_index(text: str) -> Tuple[str, str]:
    """
    Quita el número de orden inicial de un título (ej. '1 Un Desastre Llamado Valentina' -> 'Un Desastre Llamado Valentina', '1'),
    respetando números que forman parte real del título ('365 días', '30 sunsets', '5 lenguajes', '3 veces tú').
    """
    s = text.strip()
    if not s:
        return s, ""
    # Si empieza con número seguido de palabras que forman parte del título real, no quitarlo
    if re.match(
        r"^\d+\s+(?:d[ií]as|sunsets|lenguajes|sombras|a[ñn]os|meses|noches|horas|veces|cosas|motivos|razones|deseos|cartas|besos|reglas|pasos|lecciones|leyes|h[aá]bitos|citas)\b",
        s,
        re.IGNORECASE,
    ):
        return s, ""
    if re.match(r"^\d{1,3}\.\s+-\d+\s+-", s):
        m = re.match(r"^(\d{1,3})\.\s+", s)
        return (s[m.end():] if m else s), (str(int(m.group(1))) if m else "")
    m = re.match(r"^(\d{1,3}(?:[\.,_]\d{1,2})?)\s*(?:[\-\._]+\s*|\s+)", s)
    if m:
        raw_num = m.group(1).replace(",", ".").replace("_", ".")
        try:
            s_num = str(float(raw_num)) if "." in raw_num else str(int(raw_num))
        except ValueError:
            s_num = raw_num
        return s[m.end():].strip(), s_num
    return s, ""


def clean_filename_and_parse(filename: str) -> Dict[str, str]:
    """
    Limpia el nombre de archivo de un PDF o EPUB y extrae candidatos de título, autor, año e ISBN.
    Soporta todos los formatos comunes y casos especiales (guiones bajos, slugs con guiones,
    autores entre paréntesis, series intermedias, comas invertidas, etc.).
    """
    stem = fix_broken_accents(Path(filename).stem)

    # Extraer ISBN si está en el nombre del archivo
    isbns = extract_isbns(stem)
    isbn = isbns[0] if isbns else ""

    # Eliminar sufijos repetidos de copia como (1), (1)_230528_235655(1), -1(1), - copia, _copy
    s = re.sub(r"(?:_\d{6}_\d{6}|\s*-\d+|\s*\(\d+\))+\s*$", "", stem)
    s = re.sub(r"\s*[-_]\s*(?:copia|copy)\s*\d*$", "", s, flags=re.IGNORECASE)

    # Eliminar prefijos y sufijos basura comunes (scribd, vdownloaders, pdf_, _compress, holaebook, z-lib)
    s = re.sub(r"^(?:scribd\.)?vdownloaders\.com[\s_\-]+", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^pdf[\s_\-]+", "", s, flags=re.IGNORECASE)
    s = re.sub(r"[\s_\-]*(?:compress|holaebook|z[\s_\-]+lib[\s_\-]+org|z[\s_\-]+library|z-lib\.org)[\s_\-]*$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"_z$", "", s, flags=re.IGNORECASE)

    # Eliminar sufijos de IDs de biblioteca tipo _9630_r1_2, _13138_r1, _21561
    s = re.sub(r"_\d{4,6}(?:_r\d+(?:_\d+)?)?$", "", s)
    s = re.sub(r"_r\d+(?:_\d+)?$", "", s)

    # Eliminar ruido conocido (z-lib, annas-archive, dokumen.pub, etc.)
    for pat in _NOISE_PATTERNS:
        s = re.sub(pat, " ", s, flags=re.IGNORECASE)

    # Eliminar paréntesis truncados sin cerrar al final (ej. "(Alice... (Z-Librar" o "(Mark Ma")
    s = re.sub(r"\s*\([^\)]*$", "", s)

    # Extraer año entre paréntesis o corchetes ej. (2018) o [2020]
    year = ""
    year_match = re.search(r"[\(\[\-\s](19[2-9]\d|20[0-2]\d)[\)\]\-\s]?", s)
    if year_match:
        year = year_match.group(1)
        s = s.replace(year_match.group(0), " ")

    # Si el ISBN estaba en el nombre, quitarlo para limpiar el título
    if isbn:
        s = re.sub(re.escape(isbn), " ", s)

    # Quitar hashes numéricos largos al inicio o final (ej. "1029384_Libro" o "-247956125")
    s = re.sub(r"^\d{5,}[\s_\-]+", "", s)
    s = re.sub(r"[\s_\-]+\d{6,}$", "", s)

    # Quitar numeración de orden al inicio y guardar series_num para diferenciar tomos de sagas:
    s, series_num = _strip_leading_series_index(s)

    # Si es una sola palabra en CamelCase (ej. "LaChicaDelTren"), separar palabras por mayúsculas
    if " " not in s and "_" not in s and "-" not in s and re.search(r"[a-záéíóúñ][A-ZÁÉÍÓÚÑ]", s):
        s = re.sub(r"(?<=[a-záéíóúñ])(?=[A-ZÁÉÍÓÚÑ])", " ", s)

    had_underscores_only = ("_" in s) and (" - " not in s) and ("_-_" not in s)

    # Patrón especial con guiones bajos: "Riordan,_Rick_Percy_Jackson..."
    comma_author_m = re.match(r"^([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+),_([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+)_(.+)$", s)
    if comma_author_m:
        author_from_comma = f"{comma_author_m.group(2)} {comma_author_m.group(1)}"
        rest_title = comma_author_m.group(3).replace("_", " ").strip()
        return {
            "title": smart_title_case(rest_title),
            "author": smart_title_case(author_from_comma),
            "year": year,
            "isbn": isbn,
            "series_num": series_num,
        }

    # Patrón especial con guiones bajos y "_Serie_": "Ruby_Dixon_Serie_Ice_Planet_..."
    if "_Serie_" in s and " - " not in s:
        parts_serie = s.split("_Serie_", 1)
        cand_auth = parts_serie[0].replace("_", " ").strip()
        rest_title = parts_serie[1].replace("_", " ").strip(" _-")
        words_rt = rest_title.split()
        half = len(words_rt) // 2
        if half >= 2 and [w.lower() for w in words_rt[:half]] == [w.lower() for w in words_rt[half:]]:
            rest_title = " ".join(words_rt[:half])
            if series_num:
                rest_title = f"{rest_title} {series_num}"
        if looks_like_person_name(cand_auth):
            return {
                "title": smart_title_case(rest_title),
                "author": smart_title_case(cand_auth),
                "year": year,
                "isbn": isbn,
                "series_num": series_num,
            }

    # Normalizar separadores con espacios asimétricos como "Elixir- Jennifer", "Mestiza -Jennifer", "(101 dálmatas)- Serena", "Tahereh Mafi.- Shatter Me"
    s = re.sub(r"\.\-\s+", " - ", s)
    s = re.sub(r"(?<=[a-záéíóúñA-ZÁÉÍÓÚÑ\)])\-\s+(?=[A-ZÁÉÍÓÚÑ])", " - ", s)
    s = re.sub(r"(?<=[a-záéíóúñA-ZÁÉÍÓÚÑ])\s+\-(?=[A-ZÁÉÍÓÚÑ])", " - ", s)

    # Manejo de archivos en formato slug con guiones sin espacios (ej. "habitos-atomicos-james-clear" o "Persuasion-Jane_Austen" o "BOSS-EVA MUÑOZ")
    if " - " not in s and "-" in s and not re.match(r"^-\d+", s):
        hyph_parts = [p.strip() for p in s.replace("_", " ").split("-") if p.strip()]
        if len(hyph_parts) == 2:
            # Ej: "Persuasion-Jane Austen", "Marissa Meyer-Heartless", "BOSS-EVA MUÑOZ", "Puedo soñar contigo-Blue jeans"
            s = f"{hyph_parts[0]} - {hyph_parts[1]}"
        elif len(hyph_parts) >= 3:
            # Caso "Laura Sanchez Herraez-1-Peligro y pasion-Serie coincidence"
            if looks_like_person_name(hyph_parts[0]) and hyph_parts[1].isdigit():
                if not series_num:
                    series_num = hyph_parts[1]
                s = f"{hyph_parts[2]} - {hyph_parts[0]}"
            else:
                # Slug completo: "habitos-atomicos-james-clear", "El-principe-del-Sol-Claudia-Ramirez-Lomeli", "Tuya-Cuando-Quieras-2-April-Russel"
                s = " ".join(hyph_parts)
                had_underscores_only = True

    # Reemplazar guiones bajos por espacios
    s = s.replace("_", " ")
    # Eliminar corchetes o paréntesis con metadatos técnicos
    s = re.sub(r"\[[^\]]*\]", " ", s)
    s = re.sub(r"\([^\)]*(?:edici[oó]n|edition|vol|tomo|spanish|english|pdf|scan|z-lib)[^\)]*\)", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\bSpanish\s+Edition\b", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip(" –—_.,;-")

    title_guess = s
    author_guess = ""

    # Detectar si el autor viene entre paréntesis al final: "Fair Catch (Kandi Steiner)" o "Silence (FLOR. SALVADOR)"
    paren_end_m = re.search(r"^(.+?)\s*\(([^()\d]{4,40})\)\s*$", s)
    if paren_end_m and " - " not in s:
        cand_t = paren_end_m.group(1).strip(" -–—.")
        cand_a = paren_end_m.group(2).strip()
        if looks_like_person_name(cand_a):
            cand_t, extra_num = _strip_leading_series_index(cand_t)
            if not series_num and extra_num:
                series_num = extra_num
            return {
                "title": smart_title_case(cand_t),
                "author": smart_title_case(cand_a),
                "year": year,
                "isbn": isbn,
                "series_num": series_num,
            }

    # Revisar patrón "Título por Autor" o "Title by Author" antes de partir
    by_match = re.search(r"^(.+?)\s+(?:by|por)\s+([A-ZÁÉÍÓÚÑa-záéíóúñ][A-Za-záéíóúñÁÉÍÓÚÑ\.\s]{3,35})$", s, re.IGNORECASE)
    if by_match and " - " not in s:
        cand_t = by_match.group(1).strip(" -–—.")
        cand_a = by_match.group(2).strip(" -–—.")
        if looks_like_person_name(smart_title_case(cand_a)):
            return {
                "title": smart_title_case(cand_t),
                "author": smart_title_case(cand_a),
                "year": year,
                "isbn": isbn,
                "series_num": series_num,
            }

    # Separar por " - " o " – " o " — "
    sep_parts = [p.strip() for p in re.split(r"\s+[-–—]\s+", s) if p.strip()]

    if len(sep_parts) == 2:
        p0, p1 = sep_parts[0], sep_parts[1]
        p0_is_author = looks_like_person_name(smart_title_case(p0))
        p1_is_author = looks_like_person_name(smart_title_case(p1))
        if p1_is_author and not p0_is_author:
            title_guess, author_guess = p0, p1
        elif p0_is_author and not p1_is_author:
            author_guess, title_guess = p0, p1
        else:
            # Si ambos parecen nombres o ninguno, en "A - B" el último suele ser el autor
            # salvo cuando A es autor y tiene iniciales tipo "R.G. Angel"
            if p0_is_author and re.search(r"\b[A-Z]\.", p0) and not re.search(r"\b[A-Z]\.", p1):
                author_guess, title_guess = p0, p1
            else:
                title_guess, author_guess = p0, p1
    elif len(sep_parts) >= 3:
        p0_auth = looks_like_person_name(smart_title_case(sep_parts[0]))
        plast_auth = looks_like_person_name(smart_title_case(sep_parts[-1]))
        p1_auth = looks_like_person_name(smart_title_case(sep_parts[1]))
        # Caso 1: "Autor - Serie X - Título" (ej. "Neva Altaj - Perfectly Imperfect 05 - Stolen Touches" o "Tahereh Mafi - Shatter Me #2 - Liberame")
        if p0_auth and not plast_auth:
            author_guess = sep_parts[0]
            title_guess = sep_parts[-1]
        # Caso 2: "Serie/Tomo - Título - Autor" (ej. "Pll - 01 Pequeñas Mentirosas - Sara Shepard" o "The Saints 01 - Merciless Saints - Michelle Heard")
        elif plast_auth and not p0_auth:
            author_guess = sep_parts[-1]
            # Si sep_parts[0] es la sigla de la serie (ej. "Pll", "The Saints 01", "Trilogia Mount #3"), usar sep_parts[1] como título del libro
            if re.match(r"^(?:pll|trilog[ií]a\b|serie\b|saga\b|the\s+saints\b)", sep_parts[0], re.IGNORECASE):
                title_guess = " - ".join(sep_parts[1:-1])
            else:
                title_guess = sep_parts[0]
        # Caso 3: "Título - Autor - Serie #2" (ej. "Sinner - Sierra Simone - Priest #2")
        elif p1_auth and not p0_auth and not plast_auth:
            title_guess = sep_parts[0]
            author_guess = sep_parts[1]
        else:
            author_guess = sep_parts[-1]
            title_guess = " - ".join(sep_parts[:-1])

    # Si no había separador " - ", intentar extraer el autor del final (ej. "Asfixia Alex Mirez", "habitos atomicos james clear", "La promesa de Gabriel #4 Sylvain Reynard")
    if not author_guess:
        # Caso con #N en medio: "La promesa de Gabriel #4 Sylvain Reynard"
        hash_m = re.match(r"^(.+?)\s+#(\d+(?:\.\d+)?)\s+([A-ZÁÉÍÓÚÑ][A-Za-záéíóúñÁÉÍÓÚÑ\s]{3,35})$", title_guess)
        if hash_m and looks_like_person_name(smart_title_case(hash_m.group(3))):
            title_guess = hash_m.group(1).strip()
            if not series_num:
                series_num = hash_m.group(2)
            author_guess = hash_m.group(3).strip()

    if not author_guess:
        # Quitar número suelto al final si viene después del autor (ej. "Casado con mi secretaria Roxana Aguirre 1")
        trailing_num_m = re.match(r"^(.+?)\s+(\d{1,2})$", title_guess)
        work_str = trailing_num_m.group(1).strip() if trailing_num_m else title_guess
        u_words = work_str.split()
        if len(u_words) >= 3:
            # Probar últimas 2 palabras como autor
            cand_tail_2 = smart_title_case(" ".join(u_words[-2:]))
            cand_head_2 = " ".join(u_words[:-2])
            # Probar últimas 3 palabras si tiene dos apellidos hispanos (ej. "Claudia Ramirez Lomeli", "Laura Sanchez Herraez")
            cand_tail_3 = smart_title_case(" ".join(u_words[-3:])) if len(u_words) >= 5 else ""
            if cand_tail_3 and looks_like_person_name(cand_tail_3) and had_underscores_only:
                head_3 = " ".join(u_words[:-3])
                # Si justo antes del autor hay un número de tomo (ej. "Tuya Cuando Quieras 2 April Russel"), separarlo
                head_3_clean = re.sub(r"\s+\d{1,2}$", "", head_3).strip()
                title_guess = head_3_clean or head_3
                author_guess = cand_tail_3
            elif looks_like_person_name(cand_tail_2) and (had_underscores_only or len(u_words) >= 4):
                head_2_clean = re.sub(r"\s+\d{1,2}$", "", cand_head_2).strip()
                title_guess = head_2_clean or cand_head_2
                author_guess = cand_tail_2

    # Revisar patrón separado por punto: "Ladrona de Guante Negro.Anastasia Untila" o "la escala. T L Swan"
    if not author_guess and "." in title_guess:
        dot_parts = [p.strip() for p in title_guess.rsplit(".", 1)]
        if len(dot_parts) == 2 and len(dot_parts[0]) >= 3 and looks_like_person_name(smart_title_case(dot_parts[1])):
            title_guess, author_guess = dot_parts[0], dot_parts[1]

    # Limpiar numeración inicial que haya quedado dentro de title_guess tras separar el autor
    # (ej. "Grace H Desastre - 1 Un Desastre Llamado Valentina" -> "Un Desastre Llamado Valentina")
    title_guess, extra_snum = _strip_leading_series_index(title_guess)
    if not series_num and extra_snum:
        series_num = extra_snum

    # Corregir artículo invertido al final (ej. "club de los incomprendidos, El" -> "El club de los incomprendidos")
    inv_art = re.match(r"^(.+?),\s*(El|La|Los|Las|The)\s*$", title_guess, re.IGNORECASE)
    if inv_art:
        title_guess = f"{inv_art.group(2)} {inv_art.group(1)}"

    return {
        "title": smart_title_case(title_guess),
        "author": smart_title_case(author_guess),
        "year": year,
        "isbn": isbn,
        "series_num": series_num,
    }


def analyze_pdf_text_heuristics(pages_text: List[str]) -> Dict[str, str]:
    """
    Analiza el texto extraído de las primeras páginas del PDF para detectar:
    ISBN, Año de edición, Editorial, y candidatos a Título/Autor de portada.
    """
    full_text = "\n".join(pages_text)
    isbns = extract_isbns(full_text)
    isbn = isbns[0] if isbns else ""

    # Año de publicación (preferir donde aparece © o Copyright o Edición)
    year = ""
    cy = _COPYRIGHT_YEAR_REGEX.search(full_text)
    if cy:
        year = cy.group(1)
    else:
        ym = _YEAR_REGEX.findall(full_text[:3000])
        if ym:
            # Elegir un año razonable
            valid_years = [int(y) for y in ym if 1950 <= int(y) <= 2027]
            if valid_years:
                year = str(max(valid_years))

    # Editorial
    publisher = ""
    for kp in _KNOWN_PUBLISHERS:
        if re.search(r"\b" + re.escape(kp) + r"\b", full_text, re.IGNORECASE):
            publisher = kp
            break
    if not publisher:
        pm = _PUBLISHER_REGEX.search(full_text)
        if pm:
            cand = pm.group(1).strip(" .,;-")
            if 3 <= len(cand) <= 45:
                publisher = cand

    # Candidatos de portada / página de título (primeras 2 páginas con texto)
    cover_title = ""
    cover_author = ""

    # Buscar patrones explícitos en créditos: "Título original:", "Autor:"
    explicit_author = re.search(
        r"(?:Autor(?:a|es)?|Escrito por|Written by|By)[:\s]+([A-ZÁÉÍÓÚÑ][A-Za-záéíóúñÁÉÍÓÚÑ\.\s]{3,45})",
        full_text[:2500],
    )
    if explicit_author:
        cand_a = explicit_author.group(1).split("\n")[0].strip()
        if looks_like_person_name(cand_a):
            cover_author = smart_title_case(cand_a)

    # Analizar líneas limpias de las primeras 2 páginas con contenido
    non_empty_pages = [p for p in pages_text[:3] if p and len(p.strip()) > 10]
    if non_empty_pages:
        first_page_lines = [
            re.sub(r"\s+", " ", ln).strip()
            for ln in non_empty_pages[0].splitlines()
            if ln.strip()
        ]
        # Filtrar líneas que no son números de página, URLs ni cabeceras basura
        clean_lines = []
        for ln in first_page_lines[:12]:
            if len(ln) < 3 or len(ln) > 100:
                continue
            if re.match(r"^(?:p[aá]gina|page|\d+|www\.|http|isbn|©|copyright|índice|contenido|prólogo)", ln, re.IGNORECASE):
                continue
            clean_lines.append(ln)

        if clean_lines:
            # Revisar si alguna de las primeras 4 líneas es nombre de persona (autor) y otra es título
            for i, ln in enumerate(clean_lines[:4]):
                if looks_like_person_name(ln) and not cover_author:
                    cover_author = smart_title_case(ln)
                elif not cover_title and len(ln) >= 3:
                    cover_title = smart_title_case(ln)

    return {
        "isbn": isbn,
        "year": year,
        "publisher": publisher,
        "cover_title": cover_title,
        "cover_author": cover_author,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Consultas Online (Google Books API + Open Library API) con Caché Local
# ─────────────────────────────────────────────────────────────────────────────

class MetadataCache:
    def __init__(self, cache_path: Path):
        self.cache_path = cache_path
        self._data: Dict[str, dict] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        if self.cache_path.is_file():
            try:
                self._data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            except Exception:
                self._data = {}

    def save(self):
        with self._lock:
            try:
                self.cache_path.write_text(
                    json.dumps(self._data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception:
                pass

    def get(self, key: str) -> Optional[dict]:
        with self._lock:
            return self._data.get(key)

    def set(self, key: str, value: dict):
        with self._lock:
            self._data[key] = value


def _extract_title_volume_tokens(title: str) -> List[str]:
    """
    Extrae indicadores de número de tomo/volumen/parte (arábigos, romanos u ordinales)
    dentro de un título para jamás mezclar tomos distintos de una misma saga.
    """
    if not title:
        return []
    tokens: List[str] = []
    # Números arábigos (ej. "Vol 1", "Parte 2", "1.5")
    for m in re.finditer(r"\b\d+(?:[\.,]\d+)?\b", title):
        tokens.append(f"num:{m.group(0).replace(',', '.')}")
    # Números romanos en mayúsculas (ej. "Suelo sagrado I", "Suelo sagrado II", "(Extraños II)")
    for m in re.finditer(r"\b(?:I|II|III|IV|V|VI|VII|VIII|IX|X|XI|XII|XIII|XIV|XV|XVI|XVII|XVIII|XIX|XX)\b", title):
        tokens.append(f"rom:{m.group(0)}")
    # Números romanos precedidos por palabra clave aunque estén capitalizados (ej. "Vol ii", "Tomo iii")
    for m in re.finditer(
        r"\b(?:vol(?:umen)?|tomo|parte|part|libro|book|saga|serie)\s*\.?\s*(i|ii|iii|iv|v|vi|vii|viii|ix|x)\b",
        title,
        re.IGNORECASE,
    ):
        rom_tok = f"rom:{m.group(1).upper()}"
        if rom_tok not in tokens:
            tokens.append(rom_tok)
    return tokens


def query_google_books(isbn: str = "", title: str = "", author: str = "", timeout: float = 4.5) -> Optional[dict]:
    """Consulta Google Books API sin necesidad de API Key."""
    try:
        if isbn:
            q = f"isbn:{isbn}"
        else:
            if not title:
                return None
            q_parts = [f'intitle:"{title}"']
            if author:
                q_parts.append(f'inauthor:"{author}"')
            q = " ".join(q_parts)

        url = f"https://www.googleapis.com/books/v1/volumes?q={urllib.parse.quote(q)}&maxResults=5"
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
        if resp.status_code != 200:
            return None
        data = resp.json()
        items = data.get("items") or []
        if not items and not isbn and author:
            # Reintentar búsqueda combinada más flexible
            q2 = f"{title} {author}".strip()
            url2 = f"https://www.googleapis.com/books/v1/volumes?q={urllib.parse.quote(q2)}&maxResults=5"
            resp2 = requests.get(url2, timeout=timeout, headers={"User-Agent": USER_AGENT})
            if resp2.status_code == 200:
                items = resp2.json().get("items") or []

        if not items:
            return None

        best_item = None
        best_score = -1.0
        norm_target_title = normalize_for_compare(title)
        norm_target_author = normalize_for_compare(author)

        for item in items:
            vi = item.get("volumeInfo", {})
            cand_title = vi.get("title", "")
            cand_authors = vi.get("authors") or []
            cand_author_str = ", ".join(cand_authors)

            if isbn:
                best_item = vi
                best_score = 1.0
                break

            norm_ct = normalize_for_compare(cand_title)
            if not norm_ct:
                continue

            # No mezclar distintos tomos/volúmenes de una saga
            if _extract_title_volume_tokens(title) != _extract_title_volume_tokens(cand_title):
                continue

            # Calcular similitud de título estricta (evitar que el nombre de la serie coincida con todos sus tomos)
            t_sim = difflib.SequenceMatcher(None, norm_target_title, norm_ct).ratio()

            a_sim = 0.0
            if norm_target_author:
                if not cand_author_str:
                    continue
                norm_ca = normalize_for_compare(cand_author_str)
                a_sim = difflib.SequenceMatcher(None, norm_target_author, norm_ca).ratio()
                if norm_target_author in norm_ca or norm_ca in norm_target_author:
                    a_sim = max(a_sim, 0.85)
                # Si ya teníamos autor en el nombre del archivo y no coincide con el de la API, descartar
                if a_sim < 0.70:
                    continue

            if t_sim < 0.84:
                continue

            score = (t_sim * 0.7) + (a_sim * 0.3) if norm_target_author else t_sim
            if score > best_score:
                best_score = score
                best_item = vi

        # Exigir un umbral mínimo de similitud para no asignar libros erróneos
        if not best_item or (not isbn and best_score < 0.72):
            return None

        identifiers = best_item.get("industryIdentifiers") or []
        found_isbn = isbn
        for ident in identifiers:
            if ident.get("type") == "ISBN_13":
                found_isbn = ident.get("identifier", found_isbn)
                break
            elif ident.get("type") == "ISBN_10" and not found_isbn:
                found_isbn = ident.get("identifier", "")

        pub_date = best_item.get("publishedDate", "")
        year_m = re.match(r"^(\d{4})", pub_date)
        year = year_m.group(1) if year_m else ""

        categories = best_item.get("categories") or []
        subject = ", ".join(categories) if categories else ""

        full_title = best_item.get("title", "")
        subtitle = best_item.get("subtitle", "")
        if subtitle and len(full_title) < 35 and subtitle.lower() not in full_title.lower():
            # Mantener título principal limpio
            pass

        return {
            "title": full_title.strip(),
            "author": ", ".join(best_item.get("authors") or []).strip(),
            "publisher": (best_item.get("publisher") or "").strip(),
            "year": year,
            "isbn": found_isbn,
            "subject": subject,
            "language": (best_item.get("language") or "").strip(),
            "description": (best_item.get("description") or "")[:400].strip(),
            "source": "Google Books",
            "confidence": "alta" if (isbn or best_score >= 0.78) else "media",
        }
    except Exception:
        return None


def query_open_library(isbn: str = "", title: str = "", author: str = "", timeout: float = 4.5) -> Optional[dict]:
    """Consulta Open Library API como respaldo gratuito sin API key."""
    try:
        if isbn:
            url = f"https://openlibrary.org/search.json?isbn={urllib.parse.quote(isbn)}&limit=3"
        else:
            if not title:
                return None
            params = {"title": title, "limit": 5}
            if author:
                params["author"] = author
            url = f"https://openlibrary.org/search.json?{urllib.parse.urlencode(params)}"

        resp = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
        if resp.status_code != 200:
            return None
        docs = resp.json().get("docs") or []
        if not docs:
            return None

        best_doc = None
        best_score = -1.0
        norm_target_title = normalize_for_compare(title)
        norm_target_author = normalize_for_compare(author)

        for doc in docs:
            if isbn:
                best_doc = doc
                best_score = 1.0
                break
            cand_title = doc.get("title", "")
            cand_authors = doc.get("author_name") or []
            cand_author_str = ", ".join(cand_authors[:2])
            norm_ct = normalize_for_compare(cand_title)
            if not norm_ct:
                continue
            if _extract_title_volume_tokens(title) != _extract_title_volume_tokens(cand_title):
                continue
            t_sim = difflib.SequenceMatcher(None, norm_target_title, norm_ct).ratio()
            a_sim = 0.0
            if norm_target_author:
                if not cand_author_str:
                    continue
                norm_ca = normalize_for_compare(cand_author_str)
                a_sim = difflib.SequenceMatcher(None, norm_target_author, norm_ca).ratio()
                if norm_target_author in norm_ca or norm_ca in norm_target_author:
                    a_sim = max(a_sim, 0.85)
                if a_sim < 0.70:
                    continue
            if t_sim < 0.84:
                continue
            score = (t_sim * 0.7) + (a_sim * 0.3) if norm_target_author else t_sim
            if score > best_score:
                best_score = score
                best_doc = doc

        if not best_doc or (not isbn and best_score < 0.72):
            return None

        isbns = best_doc.get("isbn") or []
        found_isbn = isbn or (isbns[0] if isbns else "")
        publishers = best_doc.get("publisher") or []
        subjects = best_doc.get("subject") or []
        langs = best_doc.get("language") or []
        first_year = best_doc.get("first_publish_year")

        return {
            "title": (best_doc.get("title") or "").strip(),
            "author": ", ".join((best_doc.get("author_name") or [])[:3]).strip(),
            "publisher": (publishers[0] if publishers else "").strip(),
            "year": str(first_year) if first_year else "",
            "isbn": found_isbn,
            "subject": ", ".join(subjects[:4]).strip(),
            "language": (langs[0] if langs else "").strip(),
            "description": "",
            "source": "Open Library",
            "confidence": "alta" if (isbn or best_score >= 0.78) else "media",
        }
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Modelo de Datos del Libro PDF
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BookRecord:
    id: str
    filename: str
    filepath: str
    rel_path: str
    size_bytes: int
    size_human: str
    sha256: str
    pages: int

    # Metadatos actuales dentro del PDF
    current_title: str = ""
    current_author: str = ""
    current_subject: str = ""
    current_keywords: str = ""
    current_producer: str = ""

    # Metadatos reales detectados / sugeridos (editables por el usuario)
    real_title: str = ""
    real_author: str = ""
    year: str = ""
    publisher: str = ""
    isbn: str = ""
    subject: str = ""
    language: str = ""
    description: str = ""
    series_num: str = ""
    source: str = "Análisis Local"
    confidence: str = "media"

    # Nombre de archivo propuesto
    suggested_filename: str = ""
    needs_rename: bool = False
    needs_metadata_update: bool = False

    # Estado de duplicados
    is_duplicate: bool = False
    duplicate_group_id: str = ""
    duplicate_type: str = ""      # "exact_hash" | "same_isbn" | "same_book" | "similar_content"
    duplicate_reason: str = ""
    duplicate_of: str = ""        # filename del ejemplar principal sugerido conservar
    keep_in_group: bool = True

    # Huella de texto para comparación
    text_fingerprint: str = ""
    preview_snippet: str = ""
    analyzed: bool = False
    error: str = ""
    file_format: str = "PDF"

    def to_dict(self) -> dict:
        d = asdict(self)
        # No enviar text_fingerprint largo al frontend innecesariamente
        d.pop("text_fingerprint", None)
        return d


# ─────────────────────────────────────────────────────────────────────────────
# Construcción de Nombre Sugerido y Sanitización
# ─────────────────────────────────────────────────────────────────────────────

def build_suggested_filename(
    title: str,
    author: str,
    year: str = "",
    pattern: str = "{titulo} - {autor}",
    fallback_stem: str = "Libro",
    ext: str = ".pdf",
) -> str:
    """Genera un nombre de archivo .pdf o .epub válido en Windows según el patrón."""
    ext_clean = (ext or ".pdf").lower()
    if not ext_clean.startswith("."):
        ext_clean = "." + ext_clean
    if ext_clean not in (".pdf", ".epub"):
        ext_clean = ".pdf"

    t = (title or "").strip() or fallback_stem
    a = (author or "").strip()
    y = (year or "").strip()

    if not a:
        # Si no hay autor conocido, usar solo el título (y año si aplica)
        if "{año}" in pattern and y:
            name = f"{t} ({y})"
        else:
            name = t
    else:
        name = pattern
        name = name.replace("{titulo}", t).replace("{autor}", a)
        if "{año}" in name:
            if y:
                name = name.replace("{año}", y)
            else:
                # Limpiar paréntesis vacíos si no hay año
                name = re.sub(r"\s*[\(\[]?\{año\}[\)\]]?", "", name)

    # Reemplazar dos puntos por " - " o quitar caracteres inválidos en Windows
    name = re.sub(r"\s*:\s*", " - ", name)
    name = _INVALID_WIN_CHARS.sub("", name)
    name = re.sub(r"\s+", " ", name).strip(" .-_")
    if len(name) > 180:
        name = name[:180].strip(" .-_")
    if not name:
        name = fallback_stem
    # Quitar extensión previa si la tenía y poner la extensión correcta (.pdf o .epub)
    name = re.sub(r"\.(?:pdf|epub)$", "", name, flags=re.IGNORECASE)
    name += ext_clean
    return name


# ─────────────────────────────────────────────────────────────────────────────
# Lectura y Escritura de Metadatos EPUB (OPF / Dublin Core)
# ─────────────────────────────────────────────────────────────────────────────

def read_epub_metadata(filepath: Path, extract_text: bool = False) -> dict:
    """Extrae metadatos estándar Dublin Core (OPF) y conteo estimado de páginas de un archivo .epub."""
    res = {
        "title": "",
        "author": "",
        "subject": "",
        "publisher": "",
        "year": "",
        "isbn": "",
        "language": "",
        "producer": "",
        "pages": 0,
        "pages_text": [],
        "error": "",
    }
    try:
        with zipfile.ZipFile(filepath, "r") as zf:
            namelist = zf.namelist()
            opf_path = ""
            if "META-INF/container.xml" in namelist:
                try:
                    c_xml = zf.read("META-INF/container.xml")
                    c_root = ET.fromstring(c_xml)
                    for elem in c_root.iter():
                        if elem.tag.endswith("rootfile") and elem.attrib.get("full-path"):
                            opf_path = elem.attrib["full-path"]
                            break
                except Exception:
                    pass
            if not opf_path or opf_path not in namelist:
                opf_candidates = [n for n in namelist if n.lower().endswith(".opf")]
                if opf_candidates:
                    opf_path = opf_candidates[0]

            html_files = [
                n for n in namelist
                if n.lower().endswith((".xhtml", ".html", ".htm")) and "toc" not in n.lower()
            ]
            total_html_bytes = sum(zf.getinfo(n).file_size for n in html_files)
            res["pages"] = max(1, total_html_bytes // 2200) if html_files else 1

            if opf_path and opf_path in namelist:
                opf_bytes = zf.read(opf_path)
                root = ET.fromstring(opf_bytes)
                subjects = []
                creators = []
                identifiers = []
                for elem in root.iter():
                    tag = elem.tag.split("}")[-1].lower() if "}" in elem.tag else elem.tag.lower()
                    txt = (elem.text or "").strip()
                    if not txt:
                        continue
                    if tag == "title" and not res["title"]:
                        res["title"] = fix_broken_accents(txt)
                    elif tag == "creator":
                        creators.append(fix_broken_accents(txt))
                    elif tag == "publisher" and not res["publisher"]:
                        res["publisher"] = fix_broken_accents(txt)
                    elif tag == "subject":
                        subjects.append(fix_broken_accents(txt))
                    elif tag == "language" and not res["language"]:
                        res["language"] = txt
                    elif tag == "date" and not res["year"]:
                        ym = re.search(r"\b(19[2-9]\d|20[0-2]\d)\b", txt)
                        if ym and ym.group(1) != "0101":
                            res["year"] = ym.group(1)
                    elif tag == "identifier":
                        identifiers.append(txt)
                    elif tag == "contributor" and not res["producer"]:
                        res["producer"] = txt

                if creators:
                    res["author"] = ", ".join(creators[:2])
                if subjects:
                    res["subject"] = ", ".join(subjects[:4])
                for ident in identifiers:
                    found_isbns = extract_isbns(ident)
                    if found_isbns:
                        res["isbn"] = found_isbns[0]
                        break
                    valid = validate_isbn(ident)
                    if valid:
                        res["isbn"] = valid
                        break

            if extract_text and html_files:
                for hf in html_files[:4]:
                    try:
                        raw_html = zf.read(hf).decode("utf-8", errors="ignore")
                        clean_txt = re.sub(r"<style[^>]*>.*?</style>", " ", raw_html, flags=re.DOTALL | re.IGNORECASE)
                        clean_txt = re.sub(r"<script[^>]*>.*?</script>", " ", clean_txt, flags=re.DOTALL | re.IGNORECASE)
                        clean_txt = re.sub(r"<[^>]+>", "\n", clean_txt)
                        clean_txt = re.sub(r"\n\s*\n+", "\n", clean_txt).strip()
                        if clean_txt:
                            res["pages_text"].append(clean_txt)
                    except Exception:
                        pass
    except Exception as e:
        res["error"] = f"EPUB con errores de lectura: {e}"
    return res


def write_epub_metadata(
    filepath: Path,
    title: str,
    author: str,
    subject: str = "",
    isbn: str = "",
    publisher: str = "",
    year: str = "",
    language: str = "",
) -> Tuple[bool, str]:
    """Actualiza los metadatos estándar Dublin Core dentro del archivo .opf de un libro .epub."""
    try:
        with zipfile.ZipFile(filepath, "r") as zin:
            namelist = zin.namelist()
            opf_path = ""
            if "META-INF/container.xml" in namelist:
                try:
                    c_root = ET.fromstring(zin.read("META-INF/container.xml"))
                    for elem in c_root.iter():
                        if elem.tag.endswith("rootfile") and elem.attrib.get("full-path"):
                            opf_path = elem.attrib["full-path"]
                            break
                except Exception:
                    pass
            if not opf_path or opf_path not in namelist:
                opf_candidates = [n for n in namelist if n.lower().endswith(".opf")]
                if opf_candidates:
                    opf_path = opf_candidates[0]
            if not opf_path:
                return False, "No se encontró el archivo OPF de metadatos dentro del EPUB"

            opf_text = zin.read(opf_path).decode("utf-8", errors="ignore")

            def _set_or_insert_dc(xml_str: str, tag_name: str, value: str) -> str:
                if not value:
                    return xml_str
                esc_val = (
                    value.replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                )
                pat = re.compile(rf"(<dc:{tag_name}\b[^>]*>)(.*?)(</dc:{tag_name}>)", re.IGNORECASE | re.DOTALL)
                if pat.search(xml_str):
                    return pat.sub(rf"\g<1>{esc_val}\g<3>", xml_str, count=1)
                insert_tag = f"    <dc:{tag_name}>{esc_val}</dc:{tag_name}>\n  </metadata>"
                if "</metadata>" in xml_str:
                    return xml_str.replace("</metadata>", insert_tag, 1)
                if "</opf:metadata>" in xml_str:
                    return xml_str.replace(
                        "</opf:metadata>",
                        f"    <dc:{tag_name}>{esc_val}</dc:{tag_name}>\n  </opf:metadata>",
                        1,
                    )
                return xml_str

            if title:
                opf_text = _set_or_insert_dc(opf_text, "title", title.strip())
            if author:
                opf_text = _set_or_insert_dc(opf_text, "creator", author.strip())
            if subject:
                opf_text = _set_or_insert_dc(opf_text, "subject", subject.strip())
            if publisher:
                opf_text = _set_or_insert_dc(opf_text, "publisher", publisher.strip())
            if year:
                opf_text = _set_or_insert_dc(opf_text, "date", year.strip())
            if language:
                opf_text = _set_or_insert_dc(opf_text, "language", language.strip())

            parent_dir = filepath.parent
            fd, tmp_path_str = tempfile.mkstemp(suffix=".epub", prefix=".tmp_epub_", dir=str(parent_dir))
            os.close(fd)
            tmp_path = Path(tmp_path_str)
            try:
                with zipfile.ZipFile(tmp_path, "w") as zout:
                    for item in zin.infolist():
                        if item.filename == opf_path:
                            zout.writestr(item, opf_text.encode("utf-8"))
                        else:
                            zout.writestr(item, zin.read(item.filename))
                os.replace(str(tmp_path), str(filepath))
            except Exception as inner_e:
                if tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)
                raise inner_e
        return True, "Metadatos EPUB actualizados"
    except Exception as e:
        return False, f"Error al escribir metadatos EPUB: {e}"


# ─────────────────────────────────────────────────────────────────────────────
# Escaneo y Análisis Integral de un PDF o EPUB
# ─────────────────────────────────────────────────────────────────────────────

def make_book_id(filepath: Path) -> str:
    return hashlib.md5(str(filepath.resolve()).encode("utf-8", errors="ignore")).hexdigest()[:14]


def quick_load_pdf(
    filepath: Path,
    base_dir: Path,
    rename_pattern: str = "{titulo} - {autor}",
) -> BookRecord:
    """
    Carga instantáneamente la información básica del archivo en disco (.pdf o .epub) sin abrirlo
    ni hacer peticiones de red, para mostrar toda la biblioteca de inmediato.
    """
    stat = filepath.stat()
    size_bytes = stat.st_size
    book_id = make_book_id(filepath)
    ext = filepath.suffix.lower() or ".pdf"
    file_format = "EPUB" if ext == ".epub" else "PDF"
    try:
        rel_path = str(filepath.relative_to(base_dir))
    except ValueError:
        rel_path = filepath.name

    fn_info = clean_filename_and_parse(filepath.name)
    suggested = build_suggested_filename(
        title=fn_info["title"],
        author=fn_info["author"],
        year=fn_info["year"],
        pattern=rename_pattern,
        fallback_stem=filepath.stem,
        ext=ext,
    )

    return BookRecord(
        id=book_id,
        filename=filepath.name,
        filepath=str(filepath),
        rel_path=rel_path,
        size_bytes=size_bytes,
        size_human=format_size(size_bytes),
        sha256="",
        pages=0,
        real_title=fn_info["title"] or filepath.stem,
        real_author=fn_info["author"],
        year=fn_info["year"],
        isbn=fn_info["isbn"],
        series_num=fn_info.get("series_num", ""),
        source="Pendiente de analizar",
        confidence="baja",
        suggested_filename=suggested,
        needs_rename=(filepath.name != suggested),
        needs_metadata_update=True,
        analyzed=False,
        file_format=file_format,
    )


def compute_sha256(filepath: Path) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(131072):
            h.update(chunk)
    return h.hexdigest()


def analyze_single_pdf(
    filepath: Path,
    base_dir: Path,
    cache: MetadataCache,
    use_online: bool = True,
    rename_pattern: str = "{titulo} - {autor}",
    deep_scan: bool = False,
    force_online: bool = False,
) -> BookRecord:
    """Escanea un archivo PDF o EPUB de forma ultrarrápida, extrae metadatos locales/online y propone mejoras."""
    stat = filepath.stat()
    size_bytes = stat.st_size
    sha256 = compute_sha256(filepath)
    book_id = make_book_id(filepath)
    ext = filepath.suffix.lower() or ".pdf"
    is_epub = ext == ".epub"

    try:
        rel_path = str(filepath.relative_to(base_dir))
    except ValueError:
        rel_path = filepath.name

    rec = BookRecord(
        id=book_id,
        filename=filepath.name,
        filepath=str(filepath),
        rel_path=rel_path,
        size_bytes=size_bytes,
        size_human=format_size(size_bytes),
        sha256=sha256,
        pages=0,
        analyzed=True,
        file_format="EPUB" if is_epub else "PDF",
    )

    # 1. Análisis local instantáneo del nombre de archivo
    fn_info = clean_filename_and_parse(filepath.name)
    fn_title_valid = bool(fn_info["title"]) and not is_gibberish_title(fn_info["title"])

    pages_text: List[str] = []
    epub_meta: dict = {}

    if is_epub:
        need_epub_text = deep_scan or not (fn_title_valid and fn_info["author"])
        epub_meta = read_epub_metadata(filepath, extract_text=need_epub_text)
        if epub_meta.get("error"):
            rec.error = epub_meta["error"]
        rec.pages = epub_meta.get("pages", 0)
        rec.current_title = epub_meta.get("title", "")
        rec.current_author = epub_meta.get("author", "")
        rec.current_subject = epub_meta.get("subject", "")
        rec.current_keywords = epub_meta.get("isbn", "")
        rec.current_producer = epub_meta.get("producer", "") or epub_meta.get("publisher", "")
        pages_text = epub_meta.get("pages_text", [])
    else:
        try:
            reader = PdfReader(str(filepath), strict=False)
            if reader.is_encrypted:
                try:
                    reader.decrypt("")
                except Exception:
                    pass

            rec.pages = len(reader.pages)
            meta = reader.metadata or {}

            def _get_meta(key: str) -> str:
                val = meta.get(key)
                if val is None:
                    return ""
                return str(val).strip()

            rec.current_title = _get_meta("/Title")
            rec.current_author = _get_meta("/Author")
            rec.current_subject = _get_meta("/Subject")
            rec.current_keywords = _get_meta("/Keywords")
            rec.current_producer = _get_meta("/Producer") or _get_meta("/Creator")

            valid_meta_title_pre = not is_garbage_meta(rec.current_title, filepath.stem) and len(rec.current_title.strip()) >= 3
            valid_meta_author_pre = is_valid_author_meta(rec.current_author)
            has_both_title_and_author = bool(
                (fn_title_valid or valid_meta_title_pre)
                and (fn_info["author"] or valid_meta_author_pre)
            )

            # Optimización crítica de velocidad:
            # Descomprimir flujos de fuentes PDF (extract_text) solo cuando falta título/autor o se pide escaneo profundo
            if deep_scan or not has_both_title_and_author:
                max_scan_pages = min(rec.pages, 3)
                for i in range(max_scan_pages):
                    try:
                        txt = reader.pages[i].extract_text() or ""
                        pages_text.append(txt)
                    except Exception:
                        pages_text.append("")

        except Exception as e:
            rec.error = f"PDF con errores de lectura: {e}"

    # 2. Análisis heurístico del texto de las primeras páginas (si fue necesario extraerlo)
    if pages_text:
        txt_info = analyze_pdf_text_heuristics(pages_text)
        combined_text = "\n".join(p.strip() for p in pages_text if p.strip())
        rec.preview_snippet = re.sub(r"\s+", " ", combined_text)[:360].strip()
        rec.text_fingerprint = normalize_for_compare(combined_text[:2500])
    else:
        txt_info = {"isbn": "", "year": "", "publisher": "", "cover_title": "", "cover_author": ""}

    # 3. Consolidar mejores candidatos locales:
    local_isbn = epub_meta.get("isbn", "") or txt_info["isbn"] or fn_info["isbn"]
    if not local_isbn and rec.current_keywords:
        kw_isbns = extract_isbns(rec.current_keywords)
        if kw_isbns:
            local_isbn = kw_isbns[0]

    valid_meta_title = not is_garbage_meta(rec.current_title, filepath.stem)
    valid_meta_author = is_valid_author_meta(rec.current_author)

    # Prioridad de Título: 1) Nombre limpio del archivo (si no es texto basura sin vocales como 'hdhfhf'), 2) Metadato interno válido, 3) Portada
    local_title = fn_info["title"] if fn_title_valid else ""
    if not local_title and valid_meta_title and len(rec.current_title.strip()) >= 3:
        cleaned_mt = clean_filename_and_parse(rec.current_title)
        local_title = cleaned_mt["title"] or smart_title_case(rec.current_title)
    if not local_title:
        local_title = txt_info["cover_title"] or fn_info["title"] or filepath.stem

    # Prioridad de Autor: 1) Autor extraído del nombre del archivo, 2) Metadato interno verificado como persona real, 3) Portada verificada
    local_author = fn_info["author"]
    if not local_author and valid_meta_author:
        local_author = smart_title_case(rec.current_author)
    if not local_author and valid_meta_title and len(rec.current_title.strip()) >= 3:
        cleaned_mt_for_auth = clean_filename_and_parse(rec.current_title)
        if cleaned_mt_for_auth.get("author"):
            local_author = cleaned_mt_for_auth["author"]
    if not local_author and is_valid_author_meta(txt_info["cover_author"]):
        local_author = txt_info["cover_author"]

    local_year = epub_meta.get("year", "") or txt_info["year"] or fn_info["year"]
    local_publisher = epub_meta.get("publisher", "") or txt_info["publisher"]
    local_subject = rec.current_subject if not is_garbage_meta(rec.current_subject) else ""
    local_lang = epub_meta.get("language", "")

    rec.real_title = local_title
    rec.real_author = local_author
    rec.year = local_year
    rec.publisher = local_publisher
    rec.isbn = local_isbn
    rec.subject = local_subject
    rec.language = local_lang
    rec.series_num = fn_info.get("series_num", "")
    rec.source = "Análisis Local EPUB" if is_epub else "Análisis Local PDF"
    rec.confidence = "alta" if (local_title and local_author) else "media"

    # 4. Enriquecimiento Online inteligente (Google Books / Open Library):
    # En lotes grandes, solo hace peticiones de red si falta el Autor o falta el Título (o si ya está en caché / force_online)
    if use_online and (local_isbn or local_title):
        cache_key = f"isbn:{local_isbn}" if local_isbn else f"ta2:{normalize_for_compare(local_title)}|{normalize_for_compare(local_author)}"
        online_info = cache.get(cache_key)

        needs_online_fetch = force_online or (not local_author) or (not local_title)
        if online_info is None and needs_online_fetch:
            online_info = query_google_books(isbn=local_isbn, title=local_title, author=local_author, timeout=1.6)
            if not online_info:
                online_info = query_open_library(isbn=local_isbn, title=local_title, author=local_author, timeout=1.6)
            if online_info:
                cache.set(cache_key, online_info)

        if online_info:
            if not local_author and online_info.get("author"):
                rec.real_author = online_info["author"]
            elif online_info.get("author") and local_isbn:
                rec.real_author = online_info["author"]

            if not fn_title_valid and not valid_meta_title and online_info.get("title"):
                rec.real_title = online_info["title"]

            if online_info.get("year"):
                rec.year = online_info["year"]
            if online_info.get("publisher"):
                rec.publisher = online_info["publisher"]
            if online_info.get("isbn"):
                rec.isbn = online_info["isbn"]
            if online_info.get("subject"):
                rec.subject = online_info["subject"]
            if online_info.get("language"):
                rec.language = online_info["language"]
            if online_info.get("description"):
                rec.description = online_info["description"]
            rec.source = online_info.get("source", "Google Books")
            rec.confidence = online_info.get("confidence", "alta")

    # 5. Calcular nombre de archivo sugerido y banderas de estado
    rec.suggested_filename = build_suggested_filename(
        title=rec.real_title,
        author=rec.real_author,
        year=rec.year,
        pattern=rename_pattern,
        fallback_stem=filepath.stem,
        ext=ext,
    )
    rec.needs_rename = rec.filename != rec.suggested_filename

    # Verificar si los metadatos internos necesitan actualizarse
    rec.needs_metadata_update = (
        (rec.real_title and rec.current_title.strip() != rec.real_title.strip())
        or (rec.real_author and rec.current_author.strip() != rec.real_author.strip())
        or (rec.subject and rec.current_subject.strip() != rec.subject.strip())
        or (rec.isbn and rec.isbn not in (rec.current_keywords or ""))
    )

    return rec


# ─────────────────────────────────────────────────────────────────────────────
# Detección de Duplicados (Hash Exacto + ISBN + Título/Autor + Contenido)
# ─────────────────────────────────────────────────────────────────────────────

def _book_quality_score(b: BookRecord) -> Tuple[int, int, int, int, int]:
    """
    Puntaje para elegir cuál ejemplar conservar dentro de un grupo de duplicados:
    1. Sin errores de lectura
    2. Tiene autor real identificado
    3. Mayor cantidad de metadatos internos ya diligenciados
    4. Nombre de archivo sin "(1)", "copia", etc.
    5. Mayor tamaño de archivo (mejor resolución/calidad)
    """
    no_error = 0 if b.error else 1
    has_author = 1 if (b.real_author and b.real_author.strip()) else 0
    meta_score = int(bool(b.current_title)) + int(bool(b.current_author))
    clean_name = 0 if re.search(r"\(\d+\)|copia|copy|z-lib|annas", b.filename, re.IGNORECASE) else 1
    return (no_error, has_author, meta_score, clean_name, b.size_bytes)


def are_saga_continuations(b1: BookRecord, b2: BookRecord) -> bool:
    """
    Determina si dos registros corresponden a distintos libros/tomos/continuaciones
    de una saga para evitar falsos positivos al detectar duplicados.
    """
    # 1. Si ambos tienen ISBN identificado y son distintos -> son libros distintos
    if b1.isbn and b2.isbn and b1.isbn != b2.isbn:
        return True

    # 2. Si ambos archivos tenían numeración de serie explícita distinta al inicio (ej. "01_..." vs "04_...")
    if b1.series_num and b2.series_num and b1.series_num != b2.series_num:
        return True

    # 3. Si los títulos tienen distinta numeración arábiga o romana (ej. "Suelo sagrado I" vs "Suelo sagrado II", "Vol 1" vs "Vol 2")
    v1 = _extract_title_volume_tokens(b1.real_title)
    v2 = _extract_title_volume_tokens(b2.real_title)
    if v1 != v2:
        return True

    # 4. Si ambos fueron analizados y tienen cantidad de páginas muy distinta y texto diferente
    if b1.pages > 0 and b2.pages > 0:
        page_diff = abs(b1.pages - b2.pages)
        max_pages = max(b1.pages, b2.pages)
        if page_diff > max(15, int(0.15 * max_pages)):
            if b1.text_fingerprint and b2.text_fingerprint:
                fp_ratio = difflib.SequenceMatcher(None, b1.text_fingerprint[:1200], b2.text_fingerprint[:1200]).ratio()
                if fp_ratio < 0.75:
                    return True
            else:
                return True

    return False


def detect_duplicates(books: List[BookRecord]) -> None:
    """
    Marca en sitio (`books`) los grupos de duplicados evitando falsos positivos en sagas:
    - Nivel 1: Hash SHA-256 idéntico (100% idéntico bit a bit)
    - Nivel 2: Mismo ISBN individual válido + mismo título normalizado (sin mezclar sagas/colecciones)
    - Nivel 3: Mismo Título exacto normalizado + mismo Autor (verificando que no sean distintos tomos de una saga)
    """
    # Resetear estado previo
    for b in books:
        b.is_duplicate = False
        b.duplicate_group_id = ""
        b.duplicate_type = ""
        b.duplicate_reason = ""
        b.duplicate_of = ""
        b.keep_in_group = True

    # 0. Si dos o más libros de una saga comparten el mismo título base y autor pero tienen
    # numeración de serie inicial distinta (ej. "1. Perfectos mentirosos" y "2. Perfectos mentirosos"),
    # incorporar su número de tomo al título real y nombre propuesto para no perder la numeración de la saga
    by_base_title: Dict[Tuple[str, str], List[BookRecord]] = {}
    for b in books:
        bt = normalize_for_compare(b.real_title)
        ba = normalize_for_compare(b.real_author)
        if bt:
            by_base_title.setdefault((bt, ba), []).append(b)
    for (bt, ba), same_title_books in by_base_title.items():
        if len(same_title_books) > 1:
            distinct_series_nums = {
                b.series_num for b in same_title_books if b.series_num
            }
            if len(distinct_series_nums) >= 2:
                for b in same_title_books:
                    if b.series_num and not re.search(r"\b" + re.escape(b.series_num) + r"$", b.real_title):
                        b.real_title = f"{b.real_title} {b.series_num}"
                        b.suggested_filename = build_suggested_filename(
                            title=b.real_title,
                            author=b.real_author,
                            year=b.year,
                            fallback_stem=Path(b.filename).stem,
                            ext=Path(b.filename).suffix or ".pdf",
                        )
                        b.needs_rename = b.filename != b.suggested_filename

    assigned_ids = set()
    group_counter = 0

    def _assign_group(members: List[BookRecord], dup_type: str, reason: str):
        nonlocal group_counter
        if len(members) < 2:
            return
        group_counter += 1
        gid = f"DUP-{group_counter:03d}"
        # Ordenar por calidad descendente: el primero se conserva
        sorted_members = sorted(members, key=_book_quality_score, reverse=True)
        keeper = sorted_members[0]
        keeper.duplicate_group_id = gid
        keeper.duplicate_type = dup_type
        keeper.duplicate_reason = f"Ejemplar principal ({reason})"
        keeper.keep_in_group = True
        keeper.is_duplicate = False
        assigned_ids.add(keeper.id)

        for dup in sorted_members[1:]:
            dup.duplicate_group_id = gid
            dup.duplicate_type = dup_type
            dup.duplicate_reason = reason
            dup.duplicate_of = keeper.filename
            dup.keep_in_group = False
            dup.is_duplicate = True
            assigned_ids.add(dup.id)

    # 1. Agrupar por SHA-256 exacto (archivos 100% idénticos bit a bit)
    by_hash: Dict[str, List[BookRecord]] = {}
    for b in books:
        if not b.sha256:
            continue
        by_hash.setdefault(b.sha256, []).append(b)
    for sha, grp in by_hash.items():
        if len(grp) > 1:
            _assign_group(grp, "exact_hash", "Archivo 100% idéntico (Hash SHA-256)")

    # 2. Agrupar restantes por ISBN idéntico (solo si no son continuaciones de saga y comparten el mismo título)
    by_isbn: Dict[str, List[BookRecord]] = {}
    for b in books:
        if b.id in assigned_ids or not b.isbn:
            continue
        by_isbn.setdefault(b.isbn, []).append(b)
    for isbn_val, grp in by_isbn.items():
        if len(grp) > 1:
            sub_visited = set()
            for i in range(len(grp)):
                b1 = grp[i]
                if b1.id in sub_visited:
                    continue
                sub_cluster = [b1]
                t1 = normalize_for_compare(b1.real_title)
                for j in range(i + 1, len(grp)):
                    b2 = grp[j]
                    if b2.id in sub_visited:
                        continue
                    t2 = normalize_for_compare(b2.real_title)
                    if not are_saga_continuations(b1, b2) and t1 and t1 == t2:
                        sub_cluster.append(b2)
                        sub_visited.add(b2.id)
                if len(sub_cluster) > 1:
                    sub_visited.add(b1.id)
                    _assign_group(sub_cluster, "same_isbn", f"Mismo ISBN ({isbn_val})")

    # 3. Agrupar por Título exacto normalizado + Autor compatible (excluyendo sagas)
    remaining = [b for b in books if b.id not in assigned_ids]
    n = len(remaining)
    visited = set()

    for i in range(n):
        b1 = remaining[i]
        if b1.id in visited:
            continue
        cluster = [b1]
        reason_str = ""
        dtype_str = "same_book"

        t1 = normalize_for_compare(b1.real_title)
        a1 = normalize_for_compare(b1.real_author)
        if len(t1) < 3:
            continue

        for j in range(i + 1, n):
            b2 = remaining[j]
            if b2.id in visited:
                continue
            t2 = normalize_for_compare(b2.real_title)
            a2 = normalize_for_compare(b2.real_author)

            if len(t2) < 3 or t1 != t2:
                continue

            if are_saga_continuations(b1, b2):
                continue

            # Verificar que el autor coincida (o que al menos uno no tenga autor y el título sea específico)
            author_ok = False
            if a1 and a2:
                author_ok = (a1 == a2) or (difflib.SequenceMatcher(None, a1, a2).ratio() >= 0.85)
            elif (not a1 or not a2) and len(t1) >= 4:
                author_ok = True

            if author_ok:
                cluster.append(b2)
                visited.add(b2.id)
                reason_str = "Mismo Título y Autor"
                dtype_str = "same_book"

        if len(cluster) > 1:
            visited.add(b1.id)
            _assign_group(cluster, dtype_str, reason_str or "Mismo libro detectado")


# ─────────────────────────────────────────────────────────────────────────────
# Escritura de Metadatos PDF y Renombrado de Archivos
# ─────────────────────────────────────────────────────────────────────────────

def write_pdf_metadata_and_rename(
    filepath: Path,
    new_filename: str,
    title: str,
    author: str,
    subject: str = "",
    isbn: str = "",
    publisher: str = "",
    year: str = "",
    language: str = "",
    write_metadata: bool = True,
    do_rename: bool = True,
) -> Tuple[bool, str, Path]:
    """
    Actualiza los metadatos estándar del libro PDF (/Title, /Author, /Subject, /Keywords)
    o EPUB (Dublin Core OPF) y renombra el archivo en disco de forma segura y atómica.
    Retorna: (éxito, mensaje, nueva_ruta)
    """
    if not filepath.is_file():
        return False, "El archivo no existe en la ruta especificada", filepath

    parent_dir = filepath.parent
    orig_ext = filepath.suffix.lower() or ".pdf"
    if orig_ext not in (".pdf", ".epub"):
        orig_ext = ".pdf"
    is_epub = orig_ext == ".epub"

    # 1. Escribir metadatos PDF o EPUB si fue solicitado
    if write_metadata:
        if is_epub:
            ok_epub, msg_epub = write_epub_metadata(
                filepath=filepath,
                title=title,
                author=author,
                subject=subject,
                isbn=isbn,
                publisher=publisher,
                year=year,
                language=language,
            )
            if not ok_epub and not do_rename:
                return False, msg_epub, filepath
        else:
            try:
                reader = PdfReader(str(filepath), strict=False)
                if reader.is_encrypted:
                    try:
                        reader.decrypt("")
                    except Exception:
                        pass

                writer = PdfWriter(clone_from=reader)

                keywords_parts = []
                if isbn:
                    keywords_parts.append(f"ISBN: {isbn}")
                if publisher:
                    keywords_parts.append(f"Editorial: {publisher}")
                if year:
                    keywords_parts.append(f"Año: {year}")
                if language:
                    keywords_parts.append(f"Idioma: {language}")
                keywords_str = " | ".join(keywords_parts)

                new_meta = {
                    "/Title": (title or "").strip(),
                    "/Author": (author or "").strip(),
                    "/Subject": (subject or "").strip(),
                    "/Keywords": keywords_str,
                    "/Producer": "PDF Book Manager — DMDev Labs",
                }
                writer.add_metadata(new_meta)

                # Guardar en archivo temporal dentro de la misma carpeta y reemplazar atómicamente
                fd, tmp_path_str = tempfile.mkstemp(suffix=".pdf", prefix=".tmp_meta_", dir=str(parent_dir))
                os.close(fd)
                tmp_path = Path(tmp_path_str)
                try:
                    with open(tmp_path, "wb") as out_f:
                        writer.write(out_f)
                    os.replace(str(tmp_path), str(filepath))
                except Exception as inner_e:
                    if tmp_path.exists():
                        tmp_path.unlink(missing_ok=True)
                    raise inner_e
            except Exception as e:
                return False, f"Error al escribir metadatos PDF: {e}", filepath

    # 2. Renombrar archivo en disco si fue solicitado
    target_path = filepath
    if do_rename and new_filename and new_filename != filepath.name:
        clean_name = _INVALID_WIN_CHARS.sub("", new_filename).strip(" .")
        clean_name = re.sub(r"\.(?:pdf|epub)$", "", clean_name, flags=re.IGNORECASE)
        clean_name += orig_ext
        candidate_path = parent_dir / clean_name

        # Manejo en Windows cuando solo cambian mayúsculas/minúsculas del mismo nombre
        same_case_insensitive = filepath.name.lower() == clean_name.lower()
        if candidate_path.exists() and not same_case_insensitive:
            stem = Path(clean_name).stem
            counter = 2
            while candidate_path.exists():
                candidate_path = parent_dir / f"{stem} ({counter}){orig_ext}"
                counter += 1

        try:
            if same_case_insensitive and filepath.name != clean_name:
                tmp_rename = parent_dir / f".tmp_ren_{filepath.name}"
                os.replace(str(filepath), str(tmp_rename))
                os.replace(str(tmp_rename), str(candidate_path))
            else:
                os.replace(str(filepath), str(candidate_path))
            target_path = candidate_path
        except Exception as e:
            return False, f"Metadatos guardados, pero falló el renombrado: {e}", filepath

    return True, "Actualizado correctamente", target_path


# ─────────────────────────────────────────────────────────────────────────────
# Eliminación / Movimiento Seguro de Duplicados
# ─────────────────────────────────────────────────────────────────────────────

def remove_duplicate_file(filepath: Path, base_dir: Path, mode: str = "trash") -> Tuple[bool, str]:
    """
    Elimina o mueve un archivo duplicado según el modo elegido:
    - 'trash': Enviar a la Papelera de Reciclaje (Send2Trash)
    - 'folder': Mover a la subcarpeta `_duplicados/` dentro de `base_dir`
    - 'delete': Eliminación permanente del disco
    """
    if not filepath.is_file():
        return False, "Archivo no encontrado"

    try:
        if mode == "trash":
            try:
                from send2trash import send2trash
                send2trash(str(filepath))
                return True, "Enviado a la Papelera de reciclaje"
            except Exception:
                # Fallback seguro a carpeta _duplicados si send2trash falla
                mode = "folder"

        if mode == "folder":
            dup_dir = base_dir / "_duplicados"
            dup_dir.mkdir(parents=True, exist_ok=True)
            dest = dup_dir / filepath.name
            counter = 2
            while dest.exists():
                dest = dup_dir / f"{filepath.stem} ({counter}){filepath.suffix}"
                counter += 1
            shutil.move(str(filepath), str(dest))
            return True, f"Movido a _duplicados/{dest.name}"

        if mode == "delete":
            filepath.unlink()
            return True, "Eliminado permanentemente"

        return False, f"Modo desconocido: {mode}"
    except Exception as e:
        return False, str(e)

