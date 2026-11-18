# Cambios / Changelog

## 1.1.2 — 2026-11-18

### Compilación
- PyInstaller 6.22.3 se compila en GitHub Actions con su propio arranque (*bootloader*) en vez del precompilado del paquete, que comparten muchos programas y marcan algunos antivirus por IA (falsos positivos como el de SentinelOne en la versión anterior).
- Sin compresión UPX, que también dispara alertas heurísticas.
- Sin cambios en el funcionamiento de la app.

## 1.1.1 — 2026-11-18

### Seguridad
- **Servidor local blindado:** la API en `127.0.0.1` solo atiende a la propia ventana de la app. Rechaza peticiones de otros sitios web (CSRF) y de otros dominios (DNS rebinding), que antes podían renombrar libros o enviar duplicados a la papelera. Con 5 pruebas nuevas.

### Legal y marca
- Condiciones de uso iguales a las de [dmdevlabs.com/terminos](https://dmdevlabs.com/terminos), en español e inglés (cambian con el idioma de la app) y en `TERMINOS.md`.
- Licencia GPL-3.0 completa en `LICENSE`; marca unificada como **DMDev Labs** (también en el campo `/Producer` de los PDF).
- User-Agent propio para Google Books y Open Library, sin datos personales.
- `settings.json` (preferencias locales) y `version_info.txt` (generado) quedan fuera de git.

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
