# PDF Book Manager (v1.1.1) — Gestor de Libros PDF

Aplicación de escritorio en Python empaquetada como ejecutable `.exe` (**Flask + pywebview + SPA Tema Claro/Oscuro**) para revisar carpetas de libros en PDF, detectar y eliminar duplicados, renombrar archivos con su título y autor real, y diligenciar los metadatos internos del PDF.

Desarrollado por **DMDev Labs** — [dmdevlabs.com](https://dmdevlabs.com). Gratis y de código abierto.

## Novedades (v1.1.x)

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
   - **Análisis Local del PDF:** Limpia nombres sucios (sufijos de sitios web, hashes, guiones bajos, «copia (1)»), lee las primeras páginas del PDF y extrae ISBN-10/ISBN-13, año de edición y editorial.
   - **Consulta Online Automática:** Consulta **Google Books API** y **Open Library API** (con caché local en `metadata_cache.json`) para obtener el título oficial, autor(es), año, editorial, ISBN y categoría.

3. **Renombrado y Escritura de Metadatos PDF:**
   - Escribe los metadatos estándar directamente dentro del archivo PDF (`/Title`, `/Author`, `/Subject`, `/Keywords`).
   - Renombra los archivos según el formato elegido en la interfaz (por defecto `Título - Autor.pdf`).

## Descargas y verificación

1. Descarga `PDF Book Manager.exe` desde [Releases](../../releases/latest).
2. Cada release trae el SHA-256 del ejecutable (también en el archivo `.sha256`) y un enlace a VirusTotal.
3. El ejecutable no tiene certificado de firma comercial: Windows SmartScreen puede mostrar «Windows protegió su PC». Comprueba el SHA-256 y, si coincide, elige **Más información → Ejecutar de todas formas**.

La página del proyecto en [dmdevlabs.com/proyectos/pdf-book-manager](https://dmdevlabs.com/proyectos/pdf-book-manager) muestra las mismas descargas.

## Compilar desde el código fuente

```bat
pip install -r requirements.txt
run_app.bat      &:: ejecutar en desarrollo
build.bat        &:: genera dist\PDF Book Manager.exe y su .sha256
```

Los releases oficiales los compila GitHub Actions (`windows-latest`) con el workflow **Release**.

## Privacidad y seguridad

- Tus PDF nunca se suben: hashes, lectura de páginas y escritura de metadatos ocurren en tu equipo. Sin cuentas ni telemetría.
- Con la consulta en línea activa, solo se envía el ISBN o el título y el autor a Google Books y Open Library. Se puede usar 100% sin internet.
- Nada se renombra, modifica ni elimina sin tu confirmación; por defecto los duplicados van a la Papelera de reciclaje. Haz una copia de tu biblioteca antes de cambios masivos.
- La interfaz corre en un servidor local en `127.0.0.1` que solo atiende a la propia ventana de la app: rechaza peticiones de otros sitios web y de otros dominios.

## Licencia y condiciones de uso

- Código bajo **GNU GPL-3.0** ([LICENSE](LICENSE)).
- Se entrega «tal cual» (*as is*), sin garantías. Al usarlo aceptas las [condiciones de uso](TERMINOS.md), las mismas de [dmdevlabs.com/terminos](https://dmdevlabs.com/terminos).
- Google Books es una marca de Google LLC y Open Library un proyecto de Internet Archive. PDF Book Manager es un proyecto independiente de DMDev Labs, sin afiliación, patrocinio ni respaldo de ellos.
- Contacto: [contacto@dmdevlabs.com](mailto:contacto@dmdevlabs.com).

---

© 2026 DMDev Labs · [dmdevlabs.com](https://dmdevlabs.com)
