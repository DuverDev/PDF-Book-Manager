# Cambios / Changelog

## 1.1.0 — 2026-11-14

### Interfaz, legal y pruebas
- **Tema Claro activado por defecto** (con conmutador a Modo Oscuro en la barra superior).
- **Autoselección del primer libro (`loadFolderFast`):** al abrir una carpeta con PDFs, se selecciona automáticamente el primer ejemplar para mostrar su panel de metadatos sin clics adicionales.
- **Avisos legales y de marcas actualizados:** licencia GNU GPL-3.0, exención de responsabilidad («AS IS») y no afiliación con Google Books / Open Library.
- **Suite de pruebas unitarias (`pytest`):** 7 pruebas automatizadas sobre extracción de ISBN, limpieza de nombres, detección de duplicados y escritura de metadatos.

### Compilación y distribución
- Limpieza de `PDF Book Manager.spec` (`pathex=[]`, eliminada referencia local `.venv`).
- Eliminación de firmas locales autofirmadas (`Set-AuthenticodeSignature`) y pausa interactiva en `build.bat`.
- Compilación automatizada en GitHub Actions (`windows-latest`) con archivo `.sha256`.

## 1.0.0 — 2026-10-25

- Lanzamiento inicial con detección de duplicados en 2 niveles (hash SHA-256 e ISBN/contenido), consulta híbrida en Google Books / Open Library con caché local y escritura de metadatos PDF.
