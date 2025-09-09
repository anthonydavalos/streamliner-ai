# src/streamliner/downloader.py

import asyncio
from datetime import datetime
from pathlib import Path
from loguru import logger

from .config import AppConfig
from .storage.base import BaseStorage


class Downloader:
    """
    Gestiona la descarga de un stream en vivo, cortándolo en pequeños chunks
    de video para el procesamiento en tiempo real.
    """

    def __init__(self, config: AppConfig, storage: BaseStorage):
        self.config = config
        self.storage = storage

    async def download_stream(self, streamer: str):
        """
        Crea una tubería (pipe) entre streamlink y ffmpeg para segmentar el stream en vivo.
        Esta versión incluye un "puente" manual para compatibilidad con Windows.
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        chunk_dir_name = f"{streamer}_stream_{timestamp}"
        chunk_path = (
            Path(self.config.real_time_processing.chunk_storage_path) / chunk_dir_name
        )
        chunk_path.mkdir(parents=True, exist_ok=True)

        output_pattern = chunk_path / "chunk_%05d.ts"

        logger.info(f"Iniciando descarga en chunks para '{streamer}'.")
        logger.info(f"Directorio de chunks: {chunk_path}")

        stream_url = f"https://kick.com/{streamer}"

        streamlink_args = [
            "streamlink",
            "--stdout",
            stream_url,
            self.config.downloader.output_quality,
        ]

        ffmpeg_args = [
            "ffmpeg",
            "-i",
            "-",
            "-c",
            "copy",
            "-f",
            "segment",
            "-segment_time",
            str(self.config.real_time_processing.chunk_duration_seconds),
            "-reset_timestamps",
            "1",
            "-strftime",
            "0",
            str(output_pattern),
        ]

        logger.debug(f"Comando Streamlink: {' '.join(streamlink_args)}")
        logger.debug(f"Comando FFmpeg: {' '.join(ffmpeg_args)}")

        # --- INICIO DE LA CORRECCIÓN PARA WINDOWS ---

        # Iniciamos streamlink, su salida será una tubería que leeremos.
        streamlink_proc = await asyncio.create_subprocess_exec(
            *streamlink_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Iniciamos ffmpeg, su entrada será una tubería en la que escribiremos.
        ffmpeg_proc = await asyncio.create_subprocess_exec(
            *ffmpeg_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        logger.success(
            f"Tubería streamlink -> ffmpeg iniciada para '{streamer}'. La grabación ha comenzado."
        )

        # Función "puente" que lee de streamlink y escribe en ffmpeg.
        async def pipe_data(stream_in, stream_out):
            while True:
                chunk = await stream_in.read(4096)  # Lee en trozos de 4KB
                if not chunk:
                    break
                stream_out.write(chunk)
                await stream_out.drain()  # Espera a que el buffer se vacíe
            stream_out.close()

        async def log_stderr(process, name):
            async for line in process.stderr:
                logger.debug(f"[{name}-stderr] {line.decode(errors='ignore').strip()}")

        # Ejecutamos todo en paralelo: los dos loggers y el "puente" de datos.
        await asyncio.gather(
            log_stderr(streamlink_proc, "streamlink"),
            log_stderr(ffmpeg_proc, "ffmpeg"),
            pipe_data(streamlink_proc.stdout, ffmpeg_proc.stdin),
        )
        # --- FIN DE LA CORRECCIÓN ---

        await streamlink_proc.wait()
        await ffmpeg_proc.wait()

        if streamlink_proc.returncode != 0 or ffmpeg_proc.returncode != 0:
            logger.error(
                f"La descarga de chunks para '{streamer}' finalizó con errores."
            )
            logger.error(
                f"Código de salida de Streamlink: {streamlink_proc.returncode}"
            )
            logger.error(f"Código de salida de FFmpeg: {ffmpeg_proc.returncode}")
        else:
            logger.success(
                f"Descarga de chunks para '{streamer}' completada exitosamente."
            )
