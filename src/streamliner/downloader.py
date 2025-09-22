# src/streamliner/downloader.py

import asyncio
import subprocess
import json
from pathlib import Path
from datetime import datetime
from loguru import logger

from .config import AppConfig


class VideoDownloader:
    def __init__(self, config: AppConfig):
        self.config = config
        # CAMBIO SUGERIDO AQUÍ
        self.local_storage_base_path = config.paths.local_storage_base_dir

        self.preferred_stream_quality = "best"

        self.local_storage_base_path.mkdir(parents=True, exist_ok=True)
        logger.info(
            f"VideoDownloader inicializado. Los VODs se guardarán en: {self.local_storage_base_path}"
        )

    async def download_vod(self, url: str, streamer_name: str) -> Path:
        """
        Descarga un VOD desde la URL dada usando streamlink y lo guarda localmente.
        Retorna la ruta al archivo descargado.
        """
        # Crear un subdirectorio para el streamer dentro de local_storage_path
        streamer_vod_dir = self.local_storage_path / streamer_name
        streamer_vod_dir.mkdir(parents=True, exist_ok=True)

        logger.info(
            f"Iniciando descarga de VOD desde: {url} para {streamer_name} usando streamlink."
        )

        try:
            # Primero, usar streamlink para obtener la URL directa del stream de la mejor calidad.
            # streamlink --json <url> <quality>
            stream_info_command = [
                "streamlink",
                "--json",
                url,
                self.preferred_stream_quality,
            ]

            logger.debug(
                f"Ejecutando streamlink para obtener URL: {' '.join(stream_info_command)}"
            )
            process_info = await asyncio.create_subprocess_exec(
                *stream_info_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            stdout_info, stderr_info = await process_info.communicate()

            if process_info.returncode != 0:
                error_output = stderr_info.decode().strip()
                logger.error(
                    f"Error al obtener información del VOD con streamlink para {url}: {error_output}"
                )
                raise RuntimeError(f"Streamlink info failed: {error_output}")

            stream_data = json.loads(stdout_info.decode().strip())

            # streamlink --json devuelve un diccionario de streams disponibles o None
            # Si hay streams, 'best' debería estar presente.
            if not stream_data or not stream_data.get("streams"):
                logger.error(f"Streamlink no encontró streams válidos para {url}")
                raise RuntimeError(f"No valid streams found for {url} via Streamlink.")

            best_stream_obj = stream_data["streams"].get(self.preferred_stream_quality)
            if not best_stream_obj or not best_stream_obj.get("url"):
                logger.error(
                    f"No se pudo obtener la URL de la calidad '{self.preferred_stream_quality}' para {url}"
                )
                raise RuntimeError(
                    f"Could not get stream URL for preferred quality '{self.preferred_stream_quality}'."
                )

            actual_stream_url = best_stream_obj["url"]
            logger.info(
                f"Obtenida URL directa del VOD: {actual_stream_url[:100]}..."
            )  # Log parcial de URL

            # Generar un nombre de archivo único basado en el título del VOD si es posible,
            # o un timestamp si el título no está disponible.
            # streamlink --json no siempre proporciona un título de forma conveniente para VODs genéricos.
            # Para simplificar, usaremos un nombre basado en el streamer y un timestamp.
            # Si se requiere el título real del video, necesitaríamos una lógica más compleja
            # o una herramienta como yt-dlp para extraer metadatos sin descargar.
            # Por ahora, un timestamp es robusto.

            # Crear un nombre de archivo seguro
            filename = f"{streamer_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
            output_path = streamer_vod_dir / filename

            logger.info(f"Descargando VOD a: {output_path}")

            # Ahora, usar ffmpeg para descargar el VOD desde la URL directa
            # -i <url>: URL de entrada
            # -c copy: copiar streams de video y audio sin recodificar (más rápido)
            # -bsf:a aac_adtstoasc: filtro para corregir el formato de audio AAC si es necesario (común en HLS)
            # -movflags +faststart: para que el archivo sea reproducible antes de la descarga completa
            ffmpeg_command = [
                "ffmpeg",
                "-y",  # Sobrescribir sin preguntar
                "-i",
                actual_stream_url,
                "-c",
                "copy",
                "-bsf:a",
                "aac_adtstoasc",  # Necesario para streams HLS que usan AAC
                "-movflags",
                "+faststart",
                str(output_path),
            ]

            logger.debug(
                f"Ejecutando ffmpeg para descargar: {' '.join(ffmpeg_command)}"
            )
            process_download = await asyncio.create_subprocess_exec(
                *ffmpeg_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            stdout_download, stderr_download = await process_download.communicate()

            if process_download.returncode != 0:
                error_output = stderr_download.decode().strip()
                logger.error(
                    f"Error al descargar el VOD con ffmpeg {url}: {error_output}"
                )
                raise RuntimeError(f"FFmpeg download failed: {error_output}")
            else:
                logger.success(f"VOD descargado exitosamente: {output_path}")
                return output_path

        except FileNotFoundError:
            logger.error(
                "Comando 'streamlink' o 'ffmpeg' no encontrado. Asegúrate de que estén instalados y en tu PATH."
            )
            raise
        except Exception as e:
            logger.error(f"Error general al descargar el VOD {url}: {e}", exc_info=True)
            raise
