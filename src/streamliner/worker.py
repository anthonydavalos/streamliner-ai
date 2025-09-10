# src/streamliner/worker.py

import asyncio
from pathlib import Path
from loguru import logger
import os
from collections import deque

from .config import AppConfig
from .detector import HighlightDetector
from .pipeline import process_and_create_clip, _extract_audio


class ProcessingWorker:
    """
    Un trabajador asíncrono que vigila una carpeta en busca de nuevos chunks de video,
    los analiza, los procesa y limpia los chunks antiguos de forma segura.
    """

    def __init__(self, config: AppConfig, streamer: str, stream_session_dir: Path):
        self.config = config
        self.streamer = streamer
        self.stream_session_dir = stream_session_dir
        self.detector = HighlightDetector(config)
        self.processed_chunks = set()
        self.shutdown_event = asyncio.Event()
        # Mantiene un registro de los últimos chunks para una limpieza segura.
        # El número 5 es un margen de seguridad; puedes ajustarlo.
        self.cleanup_buffer = deque(maxlen=5)

    async def start(self):
        """Inicia el ciclo de vigilancia del trabajador."""
        logger.info(
            f"[Worker-{self.streamer}] Iniciando. Vigilando carpeta: {self.stream_session_dir}"
        )
        while not self.shutdown_event.is_set():
            try:
                all_chunks = sorted(
                    [p for p in self.stream_session_dir.glob("*.ts")],
                    key=lambda p: p.name,
                )

                new_chunks = [
                    chunk
                    for chunk in all_chunks
                    if chunk.name not in self.processed_chunks
                ]

                if new_chunks:
                    for chunk_path in new_chunks:
                        # Añadimos el chunk al buffer de limpieza ANTES de procesarlo
                        if chunk_path.exists():
                            self.cleanup_buffer.append(chunk_path)

                        # Procesamos el chunk en una tarea separada para no bloquear
                        asyncio.create_task(self.process_chunk(chunk_path))
                        self.processed_chunks.add(chunk_path.name)

                        # INTENTAMOS LIMPIAR EL CHUNK MÁS ANTIGUO DEL BUFFER
                        await self._cleanup_oldest_chunk()

                await asyncio.sleep(5)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(
                    f"[Worker-{self.streamer}] Error inesperado en el bucle principal: {e}"
                )
                await asyncio.sleep(10)

        logger.info(
            f"[Worker-{self.streamer}] Proceso de vigilancia detenido. Realizando limpieza final..."
        )
        await self._final_cleanup()

    def stop(self):
        """Señaliza al trabajador para que se detenga."""
        logger.info(f"[Worker-{self.streamer}] Recibida señal de detención.")
        self.shutdown_event.set()

    async def _cleanup_oldest_chunk(self):
        """Intenta eliminar el chunk más antiguo si el buffer está lleno."""
        if len(self.cleanup_buffer) == self.cleanup_buffer.maxlen:
            chunk_to_delete = self.cleanup_buffer[0]  # El elemento más antiguo
            await self._safe_delete(chunk_to_delete)

    async def _final_cleanup(self):
        """Limpia todos los chunks restantes en el buffer al finalizar."""
        logger.info(
            f"[Worker-{self.streamer}] Limpiando {len(self.cleanup_buffer)} chunks restantes..."
        )
        await asyncio.sleep(2)  # Espera final para que se liberen los archivos

        # Convierte deque a lista para evitar problemas al iterar y borrar
        for chunk_path in list(self.cleanup_buffer):
            await self._safe_delete(chunk_path)

        # Limpiamos los archivos restantes que no estaban en el buffer
        for chunk_path in self.stream_session_dir.glob("*.ts"):
            await self._safe_delete(chunk_path)

        logger.success(
            f"[Worker-{self.streamer}] Limpieza final de archivos completada."
        )

    async def _safe_delete(self, chunk_path: Path):
        """Elimina un archivo de forma segura, con un pequeño reintento."""
        try:
            if chunk_path.exists():
                os.remove(chunk_path)
                logger.debug(f"Chunk {chunk_path.name} limpiado exitosamente.")
        except OSError as e:
            logger.warning(
                f"No se pudo limpiar el chunk {chunk_path.name} en el primer intento: {e}"
            )
            await asyncio.sleep(1)  # Espera 1 segundo y reintenta
            try:
                if chunk_path.exists():
                    os.remove(chunk_path)
                    logger.debug(
                        f"Chunk {chunk_path.name} limpiado en el segundo intento."
                    )
            except OSError as e_retry:
                logger.error(
                    f"Fallo final al limpiar el chunk {chunk_path.name}: {e_retry}"
                )

    async def process_chunk(self, chunk_path: Path):
        """
        Ejecuta el pipeline de detección completo en un único chunk de video.
        """
        logger.info(f"[Worker-{self.streamer}] Analizando chunk: {chunk_path.name}")
        audio_chunk_path = None
        try:
            audio_chunk_path = await _extract_audio(chunk_path)
            chunk_duration = self.config.real_time_processing.chunk_duration_seconds
            highlights = await self.detector.find_highlights(
                str(audio_chunk_path), chunk_duration
            )

            if highlights:
                logger.success(
                    f"¡HIGHLIGHTS ({len(highlights)}) ENCONTRADOS EN {chunk_path.name}!"
                )
                best_highlight = highlights[0]
                logger.info(
                    f"Procediendo a crear clip para el mejor highlight (Score: {best_highlight['score']:.2f})"
                )
                await process_and_create_clip(self.config, chunk_path, self.streamer)
        except Exception as e:
            logger.error(f"Fallo al procesar el chunk {chunk_path.name}: {e}")
        finally:
            if audio_chunk_path and audio_chunk_path.exists():
                try:
                    os.remove(audio_chunk_path)
                except OSError:
                    pass
