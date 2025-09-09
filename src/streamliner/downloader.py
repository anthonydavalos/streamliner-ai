# src/streamliner/downloader.py

import asyncio
from datetime import datetime
from pathlib import Path
from loguru import logger

from .config import AppConfig
from .storage.base import BaseStorage
from .worker import ProcessingWorker


class Downloader:
    """
    Gestiona la descarga en chunks y orquesta el trabajador de procesamiento en paralelo.
    Esta versión incluye la corrección para la tubería (pipe) en Windows.
    """

    def __init__(self, config: AppConfig, storage: BaseStorage):
        self.config = config
        self.storage = storage

    async def download_stream(self, streamer: str):
        """
        Lanza el productor (streamlink -> ffmpeg) y el consumidor (ProcessingWorker)
        para procesar un stream en vivo en tiempo real.
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        chunk_dir_name = f"{streamer}_stream_{timestamp}"
        chunk_path = (
            Path(self.config.real_time_processing.chunk_storage_path) / chunk_dir_name
        )
        chunk_path.mkdir(parents=True, exist_ok=True)

        output_pattern = chunk_path / "chunk_%05d.ts"

        logger.info(f"Iniciando descarga en chunks para '{streamer}' en {chunk_path}")

        streamlink_args = [
            "streamlink",
            "--stdout",
            f"https://kick.com/{streamer}",
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

        # --- ORQUESTACIÓN DEL PRODUCTOR Y EL CONSUMIDOR CON CORRECCIÓN PARA WINDOWS ---

        # 1. Creamos nuestro trabajador de procesamiento
        worker = ProcessingWorker(self.config, streamer, chunk_path)
        worker_task = asyncio.create_task(worker.start())

        # 2. Iniciamos los procesos de descarga
        streamlink_proc = await asyncio.create_subprocess_exec(
            *streamlink_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        ffmpeg_proc = await asyncio.create_subprocess_exec(
            *ffmpeg_args,
            stdin=asyncio.subprocess.PIPE,  # Lo creamos como tubería para escribir manualmente
            stderr=asyncio.subprocess.PIPE,
        )

        logger.success(
            f"Productor (ffmpeg) y Consumidor (worker) iniciados para '{streamer}'."
        )

        # 3. Creamos las tareas de soporte
        async def pipe_data(stream_in, stream_out):
            """Función "puente" que lee de streamlink y escribe en ffmpeg."""
            while True:
                chunk = await stream_in.read(8192)  # Lee en trozos de 8KB
                if not chunk:
                    break
                try:
                    stream_out.write(chunk)
                    await stream_out.drain()
                except (BrokenPipeError, ConnectionResetError):
                    logger.warning(
                        "La tubería de ffmpeg se cerró. Probablemente el stream terminó."
                    )
                    break
            stream_out.close()

        async def log_stderr(process, name):
            async for line in process.stderr:
                logger.debug(f"[{name}-stderr] {line.decode(errors='ignore').strip()}")

        # 4. Creamos una tarea para esperar a que la descarga finalice
        async def wait_for_download_end():
            # Esperamos a que el proceso de streamlink termine (señal de que el stream acabó)
            await streamlink_proc.wait()
            # Le damos un segundo extra a la tubería para procesar los últimos datos
            await asyncio.sleep(1)
            # Forzamos la finalización de ffmpeg si no ha terminado solo
            if ffmpeg_proc.returncode is None:
                ffmpeg_proc.terminate()
            await ffmpeg_proc.wait()

        # 5. Ejecutamos todo en paralelo y esperamos a que la descarga termine
        download_task = asyncio.create_task(wait_for_download_end())

        await asyncio.gather(
            log_stderr(streamlink_proc, "streamlink"),
            log_stderr(ffmpeg_proc, "ffmpeg"),
            pipe_data(streamlink_proc.stdout, ffmpeg_proc.stdin),
            download_task,
        )

        logger.info(
            f"La descarga para '{streamer}' ha finalizado. Deteniendo al trabajador..."
        )

        # 6. Cuando la descarga termina, le decimos al trabajador que se detenga
        worker.stop()
        await worker_task

        logger.success(f"Procesamiento en tiempo real para '{streamer}' completado.")
