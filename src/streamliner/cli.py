# src/streamliner/cli.py

import asyncio
import json
import time
import click
import logging
import sys
from pathlib import Path
from typing import Optional

from loguru import logger

from .config import load_config, AppConfig
from .monitor import StreamMonitor
from .pipeline import process_single_file
from .publisher.tiktok import TikTokPublisher
from .storage import get_storage
from .downloader import VideoDownloader  # <--- Importar VideoDownloader
from pathlib import Path as _Path


# Configurar Loguru para interceptar los logs de la librería estándar
class InterceptHandler(logging.Handler):
    def emit(self, record):
        # Obtener el nombre del logger original, si es necesario para filtrado
        # logger_name = record.name
        logger_opt = logger.opt(depth=6, exception=record.exc_info)
        logger_opt.log(record.levelname, record.getMessage())


def setup_logging(config: AppConfig):
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    logger.remove()  # Eliminar el manejador por defecto de Loguru

    if config.log_json:
        logger.add(
            sys.stderr, level=config.log_level, format="{message}", serialize=True
        )
    else:
        logger.add(sys.stderr, level=config.log_level)


@click.group()
def cli():
    """Streamliner CLI para monitorear streams y procesar VODs."""
    pass


@cli.command()
@click.option(
    "--dry-run",
    is_flag=True,
    help="Ejecutar el proceso sin guardar archivos o publicar.",
)
def monitor(dry_run: bool):
    """
    Inicia el monitoreo en tiempo real de los streamers configurados.
    """
    config = load_config()
    config.dry_run = dry_run
    setup_logging(config)
    logger.info("Iniciando modo monitor...")

    monitor_instance = StreamMonitor(config, dry_run=dry_run)

    async def main():
        try:
            await monitor_instance.start_monitoring()
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.warning("Interrupción detectada. Apagando limpiamente...")
        finally:
            logger.info("Iniciando secuencia de apagado...")
            await monitor_instance.stop_monitoring()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # asyncio.run() ya maneja la interrupción, pero esto evita un traceback feo.
        logger.info("Proceso terminado por el usuario.")


@cli.command()
@click.argument("input_source")  # Puede ser URL o ruta de archivo
@click.option("--streamer", required=True, help="Nombre del streamer asociado al VOD.")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Ejecutar el proceso sin guardar archivos o publicar.",
)
def process(input_source: str, streamer: str, dry_run: bool):
    """
    Procesa un VOD desde una URL o un archivo local para generar clips.
    INPUT_SOURCE puede ser una URL de Twitch/YouTube o la ruta a un archivo de video local.
    """
    config = load_config()
    # Asignar dry_run al config general para que otros módulos puedan acceder
    config.dry_run = dry_run
    setup_logging(config)
    logger.info(f"Procesando VOD: {input_source} para el streamer: {streamer}")

    final_video_path: Optional[Path] = None

    if input_source.startswith("http://") or input_source.startswith("https://"):
        logger.info(f"Detectada URL: {input_source}. Intentando descargar VOD.")
        try:
            downloader = VideoDownloader(config)
            downloaded_path = asyncio.run(
                downloader.download_vod(
                    input_source, streamer, temp_dir=config.paths.data_dir
                )
            )
            if downloaded_path:
                final_video_path = downloaded_path
                logger.success(f"VOD descargado a: {final_video_path}")
            else:
                logger.error(f"Fallo al descargar el VOD desde la URL: {input_source}")
                sys.exit(1)
        except Exception as e:
            logger.error(
                f"Error durante la descarga del VOD desde URL: {e}", exc_info=True
            )
            sys.exit(1)
    else:
        video_path = Path(input_source).resolve()
        if not video_path.exists():
            logger.error(f"Archivo de video no encontrado: {video_path}")
            sys.exit(1)
        final_video_path = video_path

    if final_video_path:
        asyncio.run(
            process_single_file(
                config, str(final_video_path), streamer, dry_run=dry_run
            )
        )
    else:
        logger.error("No se pudo obtener una fuente de video para procesar.")
        sys.exit(1)


@cli.command()
@click.option(
    "--file",
    "file_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Ruta al archivo de video local ya renderizado (mp4).",
)
@click.option("--streamer", required=True, help="Nombre del streamer asociado.")
@click.option("--dry-run", is_flag=True, help="No subir realmente; solo simular.")
@click.option(
    "--strategy",
    type=click.Choice(
        ["AUTO", "MULTIPART", "BYTES", "DIRECT_POST"], case_sensitive=False
    ),
    default="AUTO",
    show_default=True,
    help="Estrategia de subida a usar.",
)
@click.option(
    "--no-fallback",
    is_flag=True,
    help="No usar rutas alternativas (p. ej. evitar fallback BYTES desde MULTIPART, o MULTIPART desde BYTES).",
)
def upload(
    file_path: Path, streamer: str, dry_run: bool, strategy: str, no_fallback: bool
):
    """
    Sube un archivo local ya renderizado a TikTok usando la configuración actual.
    Útil para probar la integración de subida sin ejecutar el pipeline completo.
    """
    config = load_config()
    config.dry_run = dry_run
    setup_logging(config)

    if not file_path.exists():
        logger.error(f"Archivo no encontrado: {file_path}")
        sys.exit(1)

    logger.info(
        f"Probando subida de archivo local a TikTok: {file_path} (estrategia={strategy.upper()}, fallback={'NO' if no_fallback else 'SÍ'})"
    )
    publisher = TikTokPublisher(config, get_storage(config))

    # Validación temprana: estrategia BYTES en SANDBOX deshabilitada
    if (
        strategy.upper() == "BYTES"
        and config.credentials["tiktok"].environment == "sandbox"
        and not getattr(config.publishing, "sandbox_allow_bytes_upload", False)
    ):
        logger.error(
            "Estrategia BYTES deshabilitada en sandbox. Habilita 'publishing.sandbox_allow_bytes_upload' en config.yaml bajo tu responsabilidad (posibles 403/404)."
        )
        sys.exit(1)

    async def do_upload():
        try:
            chosen = strategy.upper()
            if chosen == "BYTES":
                # Forzar subida por bytes directamente (sandbox)
                ok = await publisher.upload_video_bytes_for_sandbox(
                    str(file_path), allow_multipart_fallback=(not no_fallback)
                )
            elif chosen == "MULTIPART":
                # Forzar MULTIPART: ajustar configuración en caliente
                config.publishing.upload_strategy = "MULTIPART"
                if no_fallback:
                    # Evitar que intente BYTES o DIRECT_POST como fallback
                    setattr(config.publishing, "sandbox_allow_bytes_upload", False)
                    setattr(config.publishing, "sandbox_allow_direct_post", False)
                ok = await publisher.upload_clip(
                    str(file_path), streamer, dry_run=dry_run
                )
            elif chosen == "DIRECT_POST":
                # Intentar DIRECT_POST explícitamente (nota: en sandbox normalmente no está permitido)
                setattr(config.publishing, "sandbox_allow_direct_post", True)
                config.publishing.upload_strategy = "DIRECT_POST"
                ok = await publisher.upload_clip(
                    str(file_path), streamer, dry_run=dry_run
                )
            else:
                # AUTO: usar configuración tal cual
                ok = await publisher.upload_clip(
                    str(file_path), streamer, dry_run=dry_run
                )

            if ok:
                logger.success("Archivo subido exitosamente a TikTok.")
            else:
                logger.error("Fallo al subir el archivo a TikTok.")
                sys.exit(1)
        except Exception as e:
            logger.error(f"Error inesperado durante la subida: {e}", exc_info=True)
            sys.exit(1)

    asyncio.run(do_upload())


# Comandos utilitarios de diagnóstico/limpieza de sandbox
@cli.command(name="tiktok-diagnose")
def tiktok_diagnose():
    """Muestra el estado persistido del sandbox (spam_risk backoff, bytes_unavailable)."""
    config = load_config()
    setup_logging(config)
    state_file = _Path(config.paths.data_dir) / ".tiktok_sandbox_state.json"
    if state_file.exists():
        try:
            content = state_file.read_text(encoding="utf-8")
            logger.info(f"Estado sandbox: {content}")
        except Exception as e:
            logger.error(f"No se pudo leer el estado: {e}")
    else:
        logger.info(
            "No hay archivo de estado sandbox (aún no se han registrado eventos)."
        )


@cli.command(name="tiktok-clear-sandbox-state")
def tiktok_clear_sandbox_state():
    """Elimina el archivo de estado persistido del sandbox para reintentos desde cero."""
    config = load_config()
    setup_logging(config)
    state_file = _Path(config.paths.data_dir) / ".tiktok_sandbox_state.json"
    try:
        if state_file.exists():
            state_file.unlink()
            logger.success(f"Eliminado: {state_file}")
        else:
            logger.info("No había estado sandbox para eliminar.")
    except Exception as e:
        logger.error(f"No se pudo eliminar el estado sandbox: {e}")


@cli.command(name="upload-when-ready")
@click.option(
    "--file",
    "file_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Ruta al archivo de video local ya renderizado (mp4).",
)
@click.option("--streamer", required=True, help="Nombre del streamer asociado.")
@click.option("--dry-run", is_flag=True, help="No subir realmente; solo simular.")
@click.option(
    "--max-wait-seconds",
    type=int,
    default=7200,
    show_default=True,
    help="Tiempo máximo total a esperar por fin de backoff antes de abortar.",
)
@click.option(
    "--poll-interval",
    type=int,
    default=30,
    show_default=True,
    help="Frecuencia de sondeo del backoff (segundos).",
)
def upload_when_ready(
    file_path: Path,
    streamer: str,
    dry_run: bool,
    max_wait_seconds: int,
    poll_interval: int,
):
    """
    Espera hasta que termine el backoff por spam_risk en SANDBOX y realiza la subida automáticamente (MULTIPART por defecto).
    """
    config = load_config()
    config.dry_run = dry_run
    setup_logging(config)

    if not file_path.exists():
        logger.error(f"Archivo no encontrado: {file_path}")
        sys.exit(1)

    # Calcular tiempo restante según el estado persistido
    state_file = _Path(config.paths.data_dir) / ".tiktok_sandbox_state.json"
    last_ts = None
    if state_file.exists():
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            last_ts = data.get("last_spam_risk_ts")
        except Exception as e:
            logger.warning(f"No se pudo leer estado sandbox: {e}")

    backoff_sec = getattr(config.publishing, "sandbox_spam_backoff_seconds", 1800)
    remaining = 0
    if isinstance(last_ts, (int, float)):
        elapsed = time.time() - float(last_ts)
        remaining = int(max(0, backoff_sec - elapsed))

    if remaining > 0:
        if remaining > max_wait_seconds:
            logger.info(
                f"Backoff restante ~{remaining}s excede el máximo permitido ({max_wait_seconds}s). Abortando."
            )
            sys.exit(2)
        logger.info(
            f"Esperando a que termine el backoff (~{remaining}s). Sondeo cada {poll_interval}s..."
        )
        started = time.time()
        while True:
            elapsed_loop = int(time.time() - started)
            left = max(0, remaining - elapsed_loop)
            if left <= 0:
                break
            # Log cada ~60s para no saturar
            if left % 60 == 0:
                logger.info(f"Cuenta regresiva: ~{left}s restantes para reintentar...")
            time.sleep(max(1, min(poll_interval, left)))

    # Realizar la subida usando el publisher con la estrategia actual (AUTO/MULTIPART)
    publisher = TikTokPublisher(config, get_storage(config))

    async def do_upload():
        try:
            ok = await publisher.upload_clip(str(file_path), streamer, dry_run=dry_run)
            if ok:
                logger.success("Archivo subido exitosamente a TikTok.")
            else:
                logger.error("Fallo al subir el archivo a TikTok.")
                sys.exit(1)
        except Exception as e:
            logger.error(f"Error inesperado durante la subida: {e}", exc_info=True)
            sys.exit(1)

    asyncio.run(do_upload())


if __name__ == "__main__":
    cli()
