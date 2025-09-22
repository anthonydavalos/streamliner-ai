# src/streamliner/worker.py

import asyncio
from pathlib import Path
from loguru import logger
import os
from collections import deque
import shutil
import aiofiles
import subprocess
import stat

from .config import AppConfig
from .pipeline import process_and_create_clip, _extract_audio
from .stt import Transcriber
from .detector import HighlightDetector
from .publisher.tiktok import TikTokPublisher
from .storage import get_storage


class ProcessingWorker:
    """
    Un trabajador asíncrono que gestiona un buffer de chunks de video,
    los analiza para detectar highlights, procesa los clips y limpia los chunks.
    """

    def __init__(
        self,
        config: AppConfig,
        streamer: str,
        stream_session_dir: Path,
        dry_run: bool = False,
    ):
        self.config = config
        self.streamer = streamer
        self.stream_session_dir = stream_session_dir
        self.dry_run = dry_run

        # --- REFACTORIZACIÓN: Inicializar componentes una sola vez ---
        self.transcriber = Transcriber(
            whisper_model=config.transcription.whisper_model,
            device=config.transcription.device,
            compute_type=config.transcription.compute_type,
            data_dir=config.paths.transcriber_models_dir,
        )
        self.detector = HighlightDetector(
            config.detection,
            self.transcriber,  # Pasar la instancia del transcriber
        )
        self.storage_manager = get_storage(config)
        self.publisher = (
            TikTokPublisher(config, self.storage_manager) if config.publishing else None
        )
        # -----------------------------------------------------------

        self.highlight_buffer = deque(
            maxlen=config.real_time_processing.highlight_buffer_size
        )

        # Lock para proteger el proceso de creación de clips y evitar interrupciones.
        self.clip_processing_lock = asyncio.Lock()

        self.current_stream_time_offset = 0
        # Tarea activa para evitar solapamiento de análisis
        self._analysis_task = None  # type: asyncio.Task | None

        logger.info(
            f"[Worker-{self.streamer}] Inicializado. Directorio de sesión: {self.stream_session_dir}"
        )

    def is_processing_clip(self) -> bool:
        """Verifica si el worker está actualmente procesando un clip."""
        return self.clip_processing_lock.locked()

    async def wait_until_idle(self):
        """Espera hasta que el worker termine de procesar cualquier clip en curso."""
        async with self.clip_processing_lock:
            pass  # El bloque se ejecutará solo cuando el lock esté libre.

    async def add_chunk_for_processing(self, chunk_path: Path):
        """
        Añade un nuevo chunk al buffer y dispara la lógica de detección de highlights.
        Llamado desde StreamMonitor.
        """
        if not chunk_path.exists():
            logger.warning(f"Chunk {chunk_path.name} no existe, omitiendo.")
            return

        logger.info(
            f"[Worker-{self.streamer}] Añadiendo chunk al buffer: {chunk_path.name}"
        )
        self.highlight_buffer.append(chunk_path)

        # La lógica de offset debe considerar que el buffer no siempre se vacía por completo.
        # Es más preciso calcular el offset del chunk actual en relación con el inicio de la grabación.
        # Para simplificar por ahora, y asumiendo que el flujo es lineal, mantenemos este.
        # Para una precisión absoluta, cada chunk debería tener su timestamp de inicio real.
        self.current_stream_time_offset += (
            self.config.real_time_processing.chunk_duration_seconds
        )

        # Lanzar análisis en segundo plano solo si no hay uno activo
        if self._analysis_task is None or self._analysis_task.done():
            self._analysis_task = asyncio.create_task(
                self._process_highlights_from_buffer()
            )

    async def _process_highlights_from_buffer(self):
        """
        Une los chunks en el buffer, extrae audio, transcribe y detecta highlights.
        Si se detecta un highlight, lo corta y lo publica.
        """
        # Acceder a min_chunks_for_detection a través de self.config.real_time_processing
        if (
            len(self.highlight_buffer)
            < self.config.real_time_processing.min_chunks_for_detection
        ):
            logger.debug(
                f"Pocos chunks en buffer ({len(self.highlight_buffer)}) para detección. Mínimo: {self.config.real_time_processing.min_chunks_for_detection}"
            )
            return

        # Nombre de archivo temporal para la combinación de chunks en el buffer.
        combined_video_path_for_detection = (
            self.stream_session_dir / f"{self.streamer}_combined_for_detection.mp4"
        )

        success = await self._combine_chunks_for_detection(
            combined_video_path_for_detection
        )
        if not success:
            logger.error(
                f"[Worker-{self.streamer}] Fallo al combinar chunks para detección."
            )
            return

        logger.info(
            f"[Worker-{self.streamer}] Chunks combinados para detección: {combined_video_path_for_detection.name}"
        )

        audio_path = None
        try:
            audio_path = await _extract_audio(
                combined_video_path_for_detection, self.stream_session_dir
            )

            combined_duration_approx = (
                self.config.real_time_processing.chunk_duration_seconds
                * len(self.highlight_buffer)
            )

            highlights = await self.detector.find_highlights(
                audio_path,  # El detector ya tiene el transcriber
                combined_duration_approx,
                streamer_name=self.streamer,
                temp_dir=self.stream_session_dir,
            )

            if highlights:
                logger.success(
                    f"¡HIGHLIGHTS ({len(highlights)}) ENCONTRADOS en el buffer del streamer {self.streamer}!"
                )
                best_highlight = highlights[0]

                # Calcular el tiempo absoluto de inicio del buffer actual.
                # Es el offset total hasta el final del último chunk añadido, menos la duración total del buffer.
                buffer_start_absolute_time = (
                    self.current_stream_time_offset - combined_duration_approx
                )

                # Estos son los tiempos ABSOLUTOS en el stream completo
                highlight_start_abs = (
                    buffer_start_absolute_time + best_highlight["start_time"]
                )
                highlight_end_abs = (
                    buffer_start_absolute_time + best_highlight["end_time"]
                )

                logger.info(
                    f"Procediendo a crear clip para el mejor highlight (Score: {best_highlight['score']:.2f}). "
                    f"Tiempo ABSOLUTO en el stream: {highlight_start_abs:.2f}s - {highlight_end_abs:.2f}s. "
                    f"Tiempos RELATIVOS al buffer: {best_highlight['start_time']:.2f}s - {best_highlight['end_time']:.2f}s"
                )

                # Adquirir el lock antes de iniciar el proceso de creación del clip
                async with self.clip_processing_lock:
                    await process_and_create_clip(
                        self.config,
                        self.transcriber,  # Pasar instancia
                        self.publisher,  # Pasar instancia
                        combined_video_path_for_detection,  # <-- PASAR VIDEO COMBINADO
                        self.streamer,
                        transcription_result=best_highlight[
                            "transcription"
                        ],  # <-- PASAR TRANSCRIPCIÓN
                        buffer_start_absolute_time=buffer_start_absolute_time,  # Pasar el inicio absoluto del buffer
                        highlight_start_abs=highlight_start_abs,  # Pasar el inicio absoluto del highlight en el stream
                        highlight_end_abs=highlight_end_abs,  # Pasar el fin absoluto del highlight en el stream
                        dry_run=self.dry_run,  # <--- Usar el dry_run del worker
                        temp_dir=self.stream_session_dir,
                    )
            else:
                logger.info(
                    f"No se encontraron highlights significativos en el buffer del streamer {self.streamer}"
                )

        except Exception as e:
            # No registrar CancelledError como un error, es una interrupción normal.
            if not isinstance(e, asyncio.CancelledError):
                logger.error(
                    f"[Worker-{self.streamer}] Fallo al procesar highlights del buffer: {e}",
                    exc_info=True,
                )
        finally:
            if audio_path and audio_path.exists():
                await self._safe_delete(audio_path)
            if combined_video_path_for_detection.exists():
                await self._safe_delete(combined_video_path_for_detection)

            # --- NUEVA LÓGICA DE LIMPIEZA ---
            # Si el buffer está lleno, el chunk más antiguo ya no es necesario
            # para la siguiente ventana de detección, así que lo eliminamos.
            # El buffer `deque` con `maxlen` expulsa automáticamente el elemento más antiguo.
            # Necesitamos una forma de saber cuál fue expulsado.
            # Una forma más simple es limpiar el primer chunk si el buffer está lleno.
            if len(self.highlight_buffer) == self.highlight_buffer.maxlen:
                chunk_to_remove = self.highlight_buffer.popleft()
                logger.info(
                    f"Buffer lleno. Limpiando chunk más antiguo: {chunk_to_remove.name}"
                )
                await self._safe_delete(chunk_to_remove)
            # Señalar que el análisis terminó
            self._analysis_task = None

    async def _combine_chunks_for_detection(self, output_path: Path) -> bool:
        """
        Une los chunks de video en el highlight_buffer en un solo archivo MP4 temporal
        para el procesamiento de detección.
        """
        if not self.highlight_buffer:
            return False

        list_file_path = self.stream_session_dir / f"{self.streamer}_concat_list.txt"

        # --- NUEVO LOG AQUÍ ---
        logger.debug(
            f"[Worker-{self.streamer}] stream_session_dir: {self.stream_session_dir.as_posix()}"
        )
        logger.debug(
            f"[Worker-{self.streamer}] list_file_path original: {list_file_path.as_posix()}"
        )
        # ---------------------

        async with aiofiles.open(list_file_path, "w") as f:
            for chunk_path in self.highlight_buffer:
                # Aquí usamos la ruta absoluta del chunk, escapada para FFmpeg.
                # Path.as_posix() convierte a barras "/", que FFmpeg suele preferir incluso en Windows.
                # También se añade 'file ' al principio para el formato de la lista.
                # chunk_path ya es una Path, su as_posix() devuelve la ruta completa.

                # --- NUEVO LOG AQUÍ (dentro del bucle) ---
                logger.debug(
                    f"[Worker-{self.streamer}] Chunk path en buffer: {chunk_path.as_posix()}"
                )
                # ----------------------------------------
                chunk_absolute_path = chunk_path.resolve()
                await f.write(
                    f"file '{chunk_absolute_path.as_posix()}'\n"
                )  # <-- Esto ya es correcto para rutas absolutas

        # --- AÑADE ESTOS LOGS AQUÍ ---
        # Leer el contenido del archivo de lista para el log
        async with aiofiles.open(list_file_path, "r") as f:
            list_content = await f.read()
        logger.debug(
            f"[Worker-{self.streamer}] Contenido de {list_file_path}:\n{list_content}"
        )
        # ---------------------------

        command = [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(
                list_file_path.resolve().as_posix()
            ),  # Pasar la ruta absoluta del archivo de lista
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(
                output_path.resolve().as_posix()
            ),  # Pasar la ruta absoluta del archivo de salida
        ]

        # --- AÑADE ESTE LOG AQUÍ ---
        logger.debug(
            f"[Worker-{self.streamer}] Comando FFmpeg a ejecutar: {' '.join(command)}"
        )
        # --------------------------

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                logger.error(
                    f"FFmpeg error al concatenar chunks: {stderr.decode().strip()}"
                )
                # --- AÑADE ESTE LOG PARA VER EL STDERR COMPLETO DE FFmpeg ---
                logger.debug(f"FFmpeg STDOUT:\n{stdout.decode().strip()}")
                logger.debug(f"FFmpeg STDERR:\n{stderr.decode().strip()}")
                # -----------------------------------------------------------
                return False
            logger.debug(f"Chunks concatenados exitosamente a {output_path.name}")
            return True
        except Exception as e:
            logger.error(
                f"Excepción al concatenar chunks con FFmpeg: {e}", exc_info=True
            )
            return False
        finally:
            if list_file_path.exists():
                await self._safe_delete(list_file_path)

    async def flush_remaining_chunks(self):
        """
        Procesa los chunks que quedan en el buffer antes de que el worker se apague.
        No elimina los archivos, solo asegura que se procesen.
        """
        logger.info(
            f"[Worker-{self.streamer}] Procesando {len(self.highlight_buffer)} chunks restantes antes de terminar."
        )

        if self.highlight_buffer:
            # Ejecutar una última vez el procesamiento para los chunks que quedaron
            await self._process_highlights_from_buffer()

        # Vaciar el buffer en memoria
        self.highlight_buffer.clear()

        logger.success(
            f"[Worker-{self.streamer}] Procesamiento final de chunks completado."
        )

    async def cleanup_session(self):
        """
        Limpia todos los recursos de la sesión, incluyendo el directorio de sesión.
        Espera y verifica que todos los archivos estén cerrados antes de borrar.
        """
        logger.info(
            f"[Worker-{self.streamer}] Iniciando limpieza final del directorio de sesión: {self.stream_session_dir}"
        )

        self.highlight_buffer.clear()

        if not self.stream_session_dir or not self.stream_session_dir.exists():
            logger.warning(
                f"[Worker-{self.streamer}] El directorio de sesión no existe, no hay nada que limpiar."
            )
            return

        # Espera breve para asegurar cierre de archivos (especialmente en Windows)
        await asyncio.sleep(2)

        max_retries = 5
        retry_delay = 1.0  # segundos

        def _onerror(func, path, exc_info):
            try:
                # Quitar bit de solo lectura y reintentar
                os.chmod(path, stat.S_IWRITE)
                func(path)
            except Exception:
                pass

        for attempt in range(max_retries):
            try:
                # Borrado granular: elimina archivos primero para ayudar a Windows a liberar locks
                for root, dirs, files in os.walk(
                    self.stream_session_dir, topdown=False
                ):
                    for name in files:
                        file_path = Path(root) / name
                        try:
                            os.remove(file_path)
                        except PermissionError:
                            # Intentar quitar solo-lectura y reintentar
                            try:
                                os.chmod(file_path, stat.S_IWRITE)
                                os.remove(file_path)
                            except Exception as e:
                                logger.debug(
                                    f"No se pudo eliminar archivo en primer paso: {file_path} ({e})"
                                )
                        except Exception:
                            # Ignorar: rmtree con onerror lo manejará
                            pass
                    for name in dirs:
                        dir_path = Path(root) / name
                        try:
                            os.rmdir(dir_path)
                        except Exception:
                            pass

                shutil.rmtree(self.stream_session_dir, onerror=_onerror)
                logger.success(
                    f"[Worker-{self.streamer}] Directorio de sesión {self.stream_session_dir} limpiado exitosamente."
                )
                return
            except OSError as e:
                logger.error(
                    f"[Worker-{self.streamer}] Error al limpiar el directorio de sesión (intento {attempt + 1}/{max_retries}): {e}"
                )
                if attempt < max_retries - 1:
                    # Estrategia extra: renombrar la carpeta para soltar locks y programar borrado posterior
                    try:
                        pending = self.stream_session_dir.with_name(
                            self.stream_session_dir.name + "__pending_delete"
                        )
                        if self.stream_session_dir.exists() and not pending.exists():
                            os.rename(self.stream_session_dir, pending)
                            self.stream_session_dir = pending
                            logger.warning(
                                f"[Worker-{self.streamer}] Renombrado a {pending} para reintentar borrado."
                            )
                    except Exception:
                        pass
                    await asyncio.sleep(retry_delay)
                else:
                    logger.critical(
                        f"[Worker-{self.streamer}] Fallo persistente al limpiar el directorio de sesión {self.stream_session_dir}."
                    )

    async def _safe_delete(
        self,
        path: Path,
        max_retries: int = 3,
        retry_delay: float = 0.5,
    ):
        if not path.exists():
            return

        for attempt in range(max_retries):
            try:
                os.remove(path)
                logger.debug(f"Archivo {path.name} limpiado exitosamente.")
                return
            except OSError as e:
                if attempt < max_retries - 1:
                    logger.warning(
                        f"No se pudo eliminar {path.name} (intento {attempt + 1}/{max_retries}): {e}. Reintentando en {retry_delay}s."
                    )
                    await asyncio.sleep(retry_delay)
                else:
                    logger.error(
                        f"Fallo final al limpiar el archivo {path.name} después de {max_retries} intentos: {e}"
                    )
            except Exception as e:
                logger.error(
                    f"Error inesperado al intentar eliminar {path.name}: {e}",
                    exc_info=True,
                )
                return
