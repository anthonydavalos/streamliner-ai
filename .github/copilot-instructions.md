# Copilot Instructions for Streamliner-AI

## Visión General
Streamliner-AI es un pipeline asíncrono en Python para monitorizar streamers de Kick, detectar momentos virales, generar clips verticales y publicarlos automáticamente en TikTok. El sistema está optimizado para eficiencia y robustez, usando autenticación oficial y procesamiento selectivo de video/audio.

## Arquitectura y Componentes Clave
- **src/streamliner/**: Núcleo del pipeline. Incluye módulos para CLI (`cli.py`), configuración (`config.py`), procesamiento de video (`cutter.py`, `render.py`), detección de highlights (`detector.py`), transcripción (`stt.py`), monitorización (`monitor.py`), descarga (`downloader.py`), y publicación (`publisher/`).
- **storage/**: Abstracción para almacenamiento local y S3/R2.
- **scripts/**: Utilidades como generación de tokens para TikTok.
- **data/**: Videos, logs y resultados de procesamiento.
- **config.yaml**: Configuración principal (streamers, rutas, parámetros).
- **requirements.txt**: Dependencias de Python.

## Flujos de Trabajo Esenciales
- **Desarrollo local:**
  - Usa entorno virtual (`python -m venv venv`).
  - Instala dependencias con `pip install -r requirements.txt`.
  - Ejecuta el pipeline desde CLI:
    - Monitor: `python -m src.streamliner.cli monitor`
    - Proceso manual: `python -m src.streamliner.cli process --file "data/video.mp4" --streamer "test" --dry-run`
- **Pruebas y calidad:**
  - Linter y formateo: `ruff check .` y `ruff format .`
  - Pruebas unitarias: `pytest`
- **Docker:**
  - Construcción: `docker-compose build`
  - Ejecución: `docker-compose up -d`

## Convenciones y Patrones
- **Async-first:** Todo el pipeline usa `asyncio` y `httpx` para concurrencia eficiente.
- **Procesamiento selectivo:** Solo se transcriben segmentos de alta energía detectados por RMS.
- **Configuración por archivo:** Variables sensibles y parámetros en `.env` y `config.yaml`.
- **CLI con `click`:** Todos los comandos principales se exponen vía CLI.
- **Integración CI:** Pipeline de GitHub Actions ejecuta lint, formato y pruebas en cada push/PR.

## Integraciones y Dependencias
- **Kick API:** Autenticación OAuth2 Client Credentials. Tokens gestionados automáticamente.
- **TikTok API:** Tokens generados vía script y almacenados en `.env`.
- **FFmpeg:** Requisito de sistema para procesamiento de video/audio.
- **faster-whisper:** Transcripción eficiente de audio.

## Ejemplo de patrón clave
```python
# Ejemplo de procesamiento selectivo en detector.py
if rms > threshold:
    transcript = await transcribe_segment(audio_segment)
    # ...
```

## Archivos y directorios relevantes
- `src/streamliner/` (pipeline principal)
- `src/streamliner/publisher/tiktok.py` (publicación TikTok)
- `src/streamliner/storage/` (abstracción almacenamiento)
- `scripts/generate_tiktok_tokens.py` (tokens TikTok)
- `config.yaml`, `.env` (configuración)
- `tests/` (pruebas unitarias)

## Buenas prácticas específicas
- Mantén la lógica de I/O en funciones asíncronas.
- Usa los módulos de almacenamiento para persistencia, no acceso directo a disco.
- Actualiza los tokens y credenciales solo mediante los scripts/utilidades provistos.
- Sigue los comandos de lint y test antes de cada commit/push.

---
¿Alguna sección necesita más detalle o ejemplos? Indica qué parte del flujo, arquitectura o integración te gustaría expandir.
