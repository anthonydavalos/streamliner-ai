# src/streamliner/worker.py

import asyncio
from pathlib import Path
from loguru import logger

from .config import AppConfig
from .detector import HighlightDetector
# Más adelante importaremos Cutter, Renderer, etc.


class ProcessingWorker:
    """
    Un trabajador asíncrono que vigila una carpeta en busca de nuevos chunks de video,
    los analiza para encontrar highlights y, si los encuentra, los procesa.
    """

    def __init__(self, config: AppConfig, streamer: str, stream_session_dir: Path):
        self.config = config
        self.streamer = streamer
        self.stream_session_dir = stream_session_dir
        self.detector = HighlightDetector(config)
        self.processed_chunks = set()
        self.shutdown_event = asyncio.Event()

    async def start(self):
        """Inicia el ciclo de vigilancia del trabajador."""
        logger.info(
            f"[Worker-{self.streamer}] Iniciando. Vigilando carpeta: {self.stream_session_dir}"
        )
        while not self.shutdown_event.is_set():
            try:
                # Busca archivos .ts en el directorio
                all_chunks = sorted(
                    [p for p in self.stream_session_dir.glob("*.ts")],
                    key=lambda p: p.name,
                )

                # Filtra para encontrar solo los chunks que no hemos procesado aún
                new_chunks = [
                    chunk
                    for chunk in all_chunks
                    if chunk.name not in self.processed_chunks
                ]

                if new_chunks:
                    logger.debug(
                        f"[Worker-{self.streamer}] Se encontraron {len(new_chunks)} chunks nuevos."
                    )
                    for chunk_path in new_chunks:
                        await self.process_chunk(chunk_path)
                        self.processed_chunks.add(chunk_path.name)

                # Espera un poco antes de volver a revisar la carpeta
                await asyncio.sleep(5)

            except asyncio.CancelledError:
                logger.info(f"[Worker-{self.streamer}] Tarea cancelada. Cerrando...")
                break
            except Exception as e:
                logger.error(f"[Worker-{self.streamer}] Error inesperado: {e}")
                await asyncio.sleep(10)  # Espera más tiempo si hay un error

        logger.info(f"[Worker-{self.streamer}] Proceso de vigilancia detenido.")

    def stop(self):
        """Señaliza al trabajador para que se detenga."""
        logger.info(f"[Worker-{self.streamer}] Recibida señal de detención.")
        self.shutdown_event.set()

    async def process_chunk(self, chunk_path: Path):
        """
        Ejecuta el pipeline de detección en un único chunk de video.
        """
        logger.info(f"[Worker-{self.streamer}] Procesando chunk: {chunk_path.name}")

        # Por ahora, solo ejecutamos la detección. En la Fase 3, añadiremos el contexto
        # y la lógica de unión de chunks.
        # Necesitamos la duración del video para el detector. La obtenemos de la config.
        chunk_duration = self.config.real_time_processing.chunk_duration_seconds

        try:
            # NOTA: El detector necesita un archivo de audio. El pipeline lo extrae.
            # Vamos a simular este paso por ahora y lo integraremos bien después.
            # Por ahora, pasaremos la ruta del video y asumiremos que el detector lo maneja.
            # La lógica completa del pipeline (extraer audio, etc.) se aplicará aquí.

            # Dejaremos un log de lo que haríamos a continuación:
            logger.info(
                f"---> [SIMULACIÓN] Aquí se ejecutaría el pipeline completo para {chunk_path.name}"
            )

            # highlights = await self.detector.find_highlights(str(chunk_path), chunk_duration)
            # if highlights:
            #     logger.success(f"¡HIGHLIGHT ENCONTRADO EN {chunk_path.name}!")
            #     # Aquí iría la lógica de corte, renderizado y subida.

        except Exception as e:
            logger.error(f"Fallo al procesar el chunk {chunk_path.name}: {e}")
