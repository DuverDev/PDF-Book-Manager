# PDF Book Manager (v1.1.0) — Gestor de Libros PDF

Aplicación de escritorio en Python empaquetada como ejecutable `.exe` (**Flask + pywebview + SPA Tema Claro/Oscuro**) para revisar carpetas de libros en PDF, detectar y eliminar duplicados, renombrar archivos con su título y autor real, y diligenciar los metadatos internos del PDF.

## Novedades (v1.1.0)

- **Tema Claro activado por defecto** (con conmutador rápido a Modo Oscuro).
- **Autoselección automática:** al cargar una carpeta, selecciona de inmediato el primer libro para mostrar y editar sus metadatos al instante.
- **Avisos legales actualizados:** licencia GNU GPL-3.0, cláusula «TAL CUAL» (*AS IS*) y no afiliación con Google Books / Open Library.
- **Compilación limpia y verificada:** ejecutable generado desde GitHub Actions (`windows-latest`) sin firmas autofirmadas locales, acompañado de suma `.sha256` y pruebas unitarias `pytest`.

## Características Principales

1. **Detección y Limpieza de Duplicados:**
   - **Nivel 1 (Hash SHA-256 exacto):** Identifica archivos 100% idénticos aunque tengan nombres distintos.
   - **Nivel 2 (ISBN y Contenido/Título):** Identifica ejemplares repetidos del mismo libro por ISBN, similitud de título/autor o huella textual de las primeras páginas.
   - **Eliminación segura:** Permite revisar cada grupo de duplicados, elegir cuál ejemplar conservar y enviar las copias a la **Papelera de reciclaje**, moverlas a `_duplicados/` o eliminarlas permanentemente.

2. **Identificación Híbrida de Nombre Real y Autor:**
   - **Análisis Local del PDF:** Limpia nombres sucios (`z-lib.org`, `annas-archive`, hashes, guiones bajos), lee las primeras páginas del PDF y extrae ISBN-10/ISBN-13, año de edición y editorial.
   - **Consulta Online Automática:** Consulta **Google Books API** y **Open Library API** (con caché local en `metadata_cache.json`) para obtener el título oficial, autor(es), año, editorial, ISBN y categoría.

3. **Renombrado y Escritura de Metadatos PDF:**
   - Escribe los metadatos estándar directamente dentro del archivo PDF (`/Title`, `/Author`, `/Subject`, `/Keywords`).
   - Renombra los archivos según el formato elegido en la interfaz (por defecto `Título - Autor.pdf`).

## Uso Rápido

- **Ejecutable directo:** Abre `dist\PDF Book Manager.exe`.
- **Desde código fuente:** Ejecuta `run_app.bat`.
- **Recompilar `.exe`:** Ejecuta `build.bat`.

