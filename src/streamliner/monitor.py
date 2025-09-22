# src/streamliner/monitor.py

import asyncio
import sys
from urllib.parse import urlparse
import subprocess
import asyncio.subprocess
import json
import shutil
from pathlib import Path
from datetime import datetime
from typing import Optional

from loguru import logger

from .config import AppConfig
from .worker import ProcessingWorker
from .storage import get_storage


class StreamMonitor:
    def __init__(self, config: AppConfig, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run
        # Cambiamos la estructura para incluir el objeto worker, la tarea de grabación y el proceso ffmpeg
        self.active_streams = {}  # {streamer_login: {'worker': ProcessingWorker, 'recording_task': asyncio.Task, 'stream_session_dir': Path, 'is_live': bool, 'last_stream_url': str, 'ffmpeg_process': asyncio.Process}}
        self.chunk_duration_seconds = config.real_time_processing.chunk_duration_seconds
        self.chunk_storage_path = config.paths.chunks_dir
        self.storage_manager = get_storage(config)

        for streamer_login in config.streamers:
            self.active_streams[streamer_login] = {
                "worker": None,
                "recording_task": None,  # La tarea de asyncio que contiene el bucle de grabación
                "ffmpeg_process": None,  # El proceso ffmpeg actual (puede cambiar en cada chunk)
                "stream_session_dir": None,
                "is_live": False,
                "last_stream_url": None,
                "current_chunk_num": 0,  # Para llevar la cuenta de los chunks grabados en la sesión actual
                # Contador de strikes para evitar falsos OFFLINE por fallos puntuales de streamlink
                "offline_strikes": 0,
            }

        self.chunk_storage_path.mkdir(parents=True, exist_ok=True)
        logger.info(
            f"StreamMonitor inicializado. Chunks se guardarán en: {self.chunk_storage_path}"
        )

    async def _get_stream_info_with_streamlink(self, streamer_login: str):
        """
        Obtiene información del stream usando streamlink --json.
        Retorna el diccionario de información o None si no está en vivo o hay error.
        """
        kick_url = f"https://kick.com/{streamer_login}"
        # Usar el intérprete actual para invocar streamlink como módulo, evitando depender del PATH
        command = [
            sys.executable,
            "-m",
            "streamlink",
            "--json",
            "--default-stream",
            "best",
            kick_url,
        ]

        try:
            logger.debug(
                f"Ejecutando streamlink para {streamer_login}: {' '.join(command)}"
            )
            process = await asyncio.create_subprocess_exec(
                *command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                error_output = stderr.decode().strip()
                if "No streams found" in error_output:
                    logger.debug(
                        f"Streamlink: No se encontraron streams para {streamer_login}. Probablemente OFFLINE."
                    )
                    return None
                logger.error(f"Error streamlink para {streamer_login}: {error_output}")
                return None

            info = json.loads(stdout.decode().strip())
            return info

        except json.JSONDecodeError:
            logger.error(
                f"Error al decodificar la salida JSON de streamlink para {streamer_login}"
            )
            return None
        except FileNotFoundError:
            logger.error(
                "Comando 'streamlink' no encontrado. Asegúrate de que Streamlink esté instalado y en tu PATH."
            )
            return None
        except Exception as e:
            logger.error(
                f"Error inesperado al ejecutar streamlink para {streamer_login}: {e}"
            )
            return None

    async def _check_stream_status(
        self, streamer_login: str
    ) -> tuple[bool, str | None]:
        """
        Verifica si un streamer está en vivo usando streamlink.
        Retorna (True, stream_url) si está en vivo, (False, None) en caso contrario.
        """
        info = await self._get_stream_info_with_streamlink(streamer_login)
        if info and info.get("streams"):
            best_stream_data = info["streams"].get("best")
            if best_stream_data:
                stream_url = (
                    best_stream_data.get("url")
                    if isinstance(best_stream_data, dict)
                    else info["url"]
                )
                if stream_url:
                    logger.info(
                        f"Streamer {streamer_login} detectado ONLINE. URL: {stream_url[:50]}..."
                    )
                    return True, stream_url

        logger.debug(f"Streamer {streamer_login} detectado OFFLINE.")
        return False, None

    # --- NUEVA FUNCIÓN PARA GRABAR UN ÚNICO CHUNK ---
    async def _start_ffmpeg_chunk_recording(
        self, streamer_login: str, stream_url: str, chunk_filepath: Path
    ) -> asyncio.subprocess.Process | None:
        """Inicia un proceso FFmpeg para grabar un único chunk y lo retorna."""
        command = [
            "ffmpeg",
            "-i",
            stream_url,
            "-t",
            str(self.chunk_duration_seconds),
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-bsf:a",
            "aac_adtstoasc",
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-f",
            "mp4",
            "-movflags",
            "frag_keyframe+empty_moov",
            str(chunk_filepath),
        ]

        try:
            ffmpeg_process = await asyncio.create_subprocess_exec(
                *command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            return ffmpeg_process
        except FileNotFoundError:
            logger.error(
                "Comando 'ffmpeg' no encontrado. Asegúrate de que FFmpeg esté instalado y en tu PATH."
            )
            return None
        except Exception as e:
            logger.error(
                f"Error al iniciar FFmpeg para {streamer_login} chunk {chunk_filepath.name}: {e}",
                exc_info=True,
            )
            return None

    # --- NUEVA TAREA DE BUCLE PARA LA GRABACIÓN DE CHUNKS ---
    async def _stream_recording_loop(
        self, streamer_login: str, stream_url: str, stream_session_dir: Path
    ):
        """
        Bucle que graba chunks continuamente para un streamer.
        Esta es la tarea que se creará y se almacenará en 'recording_task'.
        """
        stream_info = self.active_streams[streamer_login]
        logger.info(
            f"Iniciando bucle de grabación para {streamer_login} desde {stream_url}"
        )

        while True:
            try:
                # Comprobar si la tarea ha sido cancelada
                if asyncio.current_task().cancelled():
                    logger.info(f"Bucle de grabación para {streamer_login} cancelado.")
                    break

                stream_info["current_chunk_num"] += 1
                chunk_filepath = (
                    stream_session_dir
                    / f"{streamer_login}_chunk_{stream_info['current_chunk_num']:05d}.mp4"
                )
                logger.info(
                    f"Grabando chunk {stream_info['current_chunk_num']} ({self.chunk_duration_seconds}s) para {streamer_login} en {chunk_filepath}"
                )

                # Iniciar el proceso FFmpeg para este chunk
                ffmpeg_process = await self._start_ffmpeg_chunk_recording(
                    streamer_login, stream_url, chunk_filepath
                )
                stream_info["ffmpeg_process"] = (
                    ffmpeg_process  # Almacenar el proceso real
                )

                if ffmpeg_process is None:
                    logger.error(
                        f"No se pudo iniciar FFmpeg para {streamer_login}. Terminando grabación."
                    )
                    break  # Salir del bucle si FFmpeg no se pudo iniciar

                (
                    stdout,
                    stderr,
                ) = await ffmpeg_process.communicate()  # Esperar a que termine

                if ffmpeg_process.returncode != 0:
                    error_output = stderr.decode().strip()
                    logger.error(
                        f"Error al grabar chunk para {streamer_login} (ffmpeg): {error_output}. "
                        "Intentando recuperar en el próximo ciclo de monitoreo."
                    )
                    # No rompemos el bucle aquí. El monitor principal detectará el problema
                    # y si la URL es inválida, reiniciará la grabación.
                    # Si el error es temporal, el siguiente chunk podría funcionar.
                else:
                    logger.success(
                        f"Chunk {stream_info['current_chunk_num']} grabado: {chunk_filepath}"
                    )
                    worker = stream_info["worker"]
                    if worker:
                        # Desacoplar: no bloquear la grabación mientras se procesa
                        asyncio.create_task(
                            worker.add_chunk_for_processing(chunk_filepath)
                        )
                    else:
                        logger.error(
                            f"No hay worker activo para {streamer_login} al añadir chunk."
                        )

                # No es necesario un sleep aquí. El bucle continuará tan pronto como ffmpeg termine.

            except asyncio.CancelledError:
                logger.info(
                    f"Bucle de grabación para {streamer_login} fue cancelado limpiamente."
                )
                break
            except Exception as e:
                logger.error(
                    f"Error inesperado en el bucle de grabación para {streamer_login}: {e}",
                    exc_info=True,
                )
                # Intenta continuar, el monitor principal eventualmente detectará si el stream murió.
                await asyncio.sleep(
                    5
                )  # Pequeña pausa para evitar un bucle de errores rápido

        logger.info(f"Finalizando bucle de grabación para {streamer_login}.")
        # Al salir del bucle (por cancelación o error), aseguramos que el proceso ffmpeg esté terminado.
        # Esta lógica ahora se maneja en _handle_stream_online y _handle_stream_offline
        # para asegurar que se ejecute en el momento correcto.
        pass

    async def _handle_stream_online(self, streamer_login: str, stream_url: str):
        """Inicia (o reinicia) la grabación y procesamiento para un streamer."""
        stream_info = self.active_streams[streamer_login]
        worker = stream_info.get("worker")

        # --- NUEVA LÓGICA DE SINCRONIZACIÓN ---
        # Si hay un worker y está ocupado procesando un clip, esperamos.
        if worker and worker.is_processing_clip():
            logger.warning(
                f"El worker de {streamer_login} está procesando un clip. "
                "El reinicio de la grabación esperará a que termine."
            )
            await worker.wait_until_idle()
            logger.info(
                f"El worker de {streamer_login} ha terminado. "
                "Procediendo con el reinicio de la grabación."
            )
        # -----------------------------------------

        # Lógica para detener la grabación anterior si existe
        if stream_info["recording_task"] is not None:
            logger.warning(
                f"Cancelando tarea de grabación anterior para {streamer_login}."
            )
            stream_info["recording_task"].cancel()
            try:
                await stream_info["recording_task"]
            except asyncio.CancelledError:
                logger.info(
                    f"Tarea de grabación anterior para {streamer_login} cancelada exitosamente."
                )
            except Exception as e:
                logger.error(
                    f"Error al esperar la cancelación de la tarea de grabación para {streamer_login}: {e}"
                )
            finally:
                stream_info["recording_task"] = None
                # Asegurarse de que cualquier proceso FFmpeg asociado también esté terminado
                if (
                    stream_info["ffmpeg_process"]
                    and stream_info["ffmpeg_process"].returncode is None
                ):
                    stream_info["ffmpeg_process"].terminate()
                    await stream_info["ffmpeg_process"].wait()
                stream_info["ffmpeg_process"] = None

        if not stream_info["is_live"]:  # Si estaba offline y ahora está online
            logger.success(f"Streamer {streamer_login} ¡ONLINE! Iniciando grabación...")

            # Crear directorio de sesión si no existe (primera vez que se detecta online)
            stream_session_dir = (
                self.chunk_storage_path
                / f"{streamer_login}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            )
            stream_session_dir.mkdir(parents=True, exist_ok=True)
            logger.info(
                f"Creando directorio de sesión para {streamer_login}: {stream_session_dir}"
            )
            stream_info["stream_session_dir"] = stream_session_dir
            stream_info["current_chunk_num"] = (
                0  # Resetear contador de chunks para la nueva sesión
            )

            # Inicializar el worker para esta sesión
            # Inicializar el worker para esta sesión
            stream_info["worker"] = ProcessingWorker(
                self.config,
                streamer_login,
                stream_session_dir,
                dry_run=self.dry_run,
            )
        else:  # Ya estaba online, pero la URL cambió o hubo un error en la grabación anterior
            logger.warning(
                f"Reiniciando grabación para {streamer_login} debido a cambio de URL o error."
            )
            # Reutilizamos el mismo stream_session_dir y worker.
            # El worker se encargará de cualquier limpieza interna si es necesario.
            # No resetear current_chunk_num si estamos reutilizando la sesión para no perder la numeración continua.
            # Sin embargo, si la URL cambia, puede que queramos resetearlo para una nueva "sub-sesión" de chunks.
            # Por simplicidad, por ahora lo mantenemos.

        stream_info["is_live"] = True
        stream_info["last_stream_url"] = stream_url

        # Iniciar la nueva tarea de bucle de grabación para el streamer
        recording_task = asyncio.create_task(
            self._stream_recording_loop(
                streamer_login, stream_url, stream_info["stream_session_dir"]
            )
        )
        stream_info["recording_task"] = (
            recording_task  # Almacenar la Tarea (no el proceso FFmpeg)
        )

    async def _handle_stream_offline(self, streamer_login: str):
        """Detiene la grabación y procesa/limpia recursos para un streamer que se puso offline."""
        stream_info = self.active_streams[streamer_login]

        if stream_info["is_live"]:
            stream_info["is_live"] = False
            stream_info["last_stream_url"] = None
            logger.warning(f"Streamer {streamer_login} OFFLINE. Deteniendo grabación.")

            # --- NUEVA LÓGICA DE SINCRONIZACIÓN AL APAGAR ---
            worker = stream_info.get("worker")
            if worker and worker.is_processing_clip():
                logger.warning(
                    f"El worker de {streamer_login} está procesando un clip. "
                    "La limpieza final esperará a que termine."
                )
                await worker.wait_until_idle()
                logger.info(
                    f"El worker de {streamer_login} ha terminado. Procediendo con la limpieza."
                )
            # -----------------------------------------------

            # 1. Cancelar la tarea de grabación de FFmpeg (el bucle)
            recording_task = stream_info["recording_task"]
            ffmpeg_process = stream_info.get("ffmpeg_process")

            if recording_task:
                # --- CORRECCIÓN PARA LIBERAR RECURSOS ---
                # Terminar el proceso FFmpeg de grabación explícitamente ANTES de cancelar la tarea.
                # Esto asegura que el handle del archivo del chunk se libere inmediatamente.
                if ffmpeg_process and ffmpeg_process.returncode is None:
                    logger.warning(
                        f"Terminando explícitamente el proceso de grabación FFmpeg para {streamer_login}."
                    )
                    ffmpeg_process.terminate()
                    await ffmpeg_process.wait()
                # -----------------------------------------

                recording_task.cancel()
                try:
                    await recording_task
                except asyncio.CancelledError:
                    logger.info(f"Tarea de grabación para {streamer_login} cancelada.")
                except Exception as e:
                    logger.error(
                        f"Error al esperar la tarea de grabación cancelada para {streamer_login}: {e}"
                    )
                finally:
                    stream_info["recording_task"] = None  # La tarea ya terminó
                    stream_info["ffmpeg_process"] = None  # El proceso ya terminó

            # 2. Indicar al worker que vacíe sus chunks y limpie
            worker = stream_info["worker"]
            if worker:
                logger.info(
                    f"Notificando al worker de {streamer_login} para vaciar y limpiar."
                )
                await worker.flush_remaining_chunks()  # Procesar lo que queda
                await worker.cleanup_session()  # Delegar la limpieza del directorio
                stream_info["worker"] = None

            # La limpieza del directorio ahora es manejada por el worker.
            # Eliminamos la llamada a _cleanup_session_dir de aquí.

            stream_info["stream_session_dir"] = None  # Resetear el directorio de sesión
            stream_info["current_chunk_num"] = 0  # Resetear contador

    async def _cleanup_session_dir(self, stream_session_dir: Path):
        """
        Elimina el directorio de sesión después de que el stream termina y se procesan los chunks.
        Implementa reintentos para manejar errores de acceso en Windows.
        """
        if not stream_session_dir.exists():
            return  # Ya no existe

        logger.info(f"Limpiando directorio de sesión: {stream_session_dir}")
        max_retries = 5
        retry_delay_seconds = 1

        for attempt in range(max_retries):
            try:
                # Esperar un poco antes de intentar la limpieza para asegurar que los handles estén liberados
                await asyncio.sleep(0.5)

                # shutil.rmtree es más robusto para eliminar directorios y su contenido
                shutil.rmtree(stream_session_dir)
                logger.success(
                    f"Directorio de sesión {stream_session_dir} limpiado exitosamente."
                )
                return  # Éxito, salir de la función
            except OSError as e:
                logger.error(
                    f"Error al limpiar el directorio de sesión {stream_session_dir} (intento {attempt + 1}/{max_retries}): {e}"
                )
                if attempt < max_retries - 1:
                    logger.info(
                        f"Reintentando limpieza en {retry_delay_seconds} segundos..."
                    )
                    await asyncio.sleep(retry_delay_seconds)
                else:
                    logger.critical(
                        f"Fallo persistente al limpiar el directorio de sesión {stream_session_dir} después de {max_retries} intentos."
                    )
            except Exception as e:
                logger.error(
                    f"Error inesperado durante la limpieza del directorio {stream_session_dir}: {e}",
                    exc_info=True,
                )
                break  # Salir en caso de un error inesperado no relacionado con OSError

    async def start_monitoring(
        self,
    ):  # La función _cleanup_session_dir ya no es necesaria y puede ser eliminada.
        """Inicia el monitoreo de streams por polling."""
        logger.info("Iniciando monitoreo de streams por polling...")
        try:
            while True:
                tasks = []
                for streamer_login in self.config.streamers:
                    tasks.append(self._monitor_single_streamer(streamer_login))

                await asyncio.gather(*tasks)
                await asyncio.sleep(self.config.monitoring.check_interval_seconds)
        finally:
            await self.stop_monitoring()

    async def _monitor_single_streamer(self, streamer_login: str):
        """Lógica para monitorear un único streamer."""
        try:
            is_live_now, current_stream_url = await self._check_stream_status(
                streamer_login
            )
            stream_info = self.active_streams[streamer_login]
            was_live = stream_info["is_live"]
            last_stream_url = stream_info["last_stream_url"]

            if is_live_now and not was_live:
                # El streamer se acaba de poner online
                stream_info["offline_strikes"] = 0  # reset strikes
                await self._handle_stream_online(streamer_login, current_stream_url)
            elif not is_live_now and was_live:
                # Histeresis: requiere 2 strikes consecutivos para marcar offline
                stream_info["offline_strikes"] += 1
                if stream_info["offline_strikes"] >= 2:
                    # El streamer se acaba de poner offline
                    await self._handle_stream_offline(streamer_login)
                    stream_info["offline_strikes"] = 0
                else:
                    logger.debug(
                        f"Posible OFFLINE detectado para {streamer_login}, strike {stream_info['offline_strikes']}/2. Esperando confirmación en el siguiente ciclo."
                    )
            elif is_live_now and was_live:
                # Está online y ya lo sabíamos, pero su URL de stream podría haber cambiado
                def get_main_domain(url: Optional[str]) -> Optional[str]:
                    if not url:
                        return None
                    hostname = urlparse(url).hostname
                    if not hostname:
                        return None
                    # Devuelve 'dominio.tld' (ej. 'live-video.net') para ignorar cambios de subdominio como sae11, sae12, etc.
                    parts = hostname.split(".")
                    return ".".join(parts[-2:]) if len(parts) >= 2 else hostname

                last_main_domain = get_main_domain(last_stream_url)
                current_main_domain = get_main_domain(current_stream_url)

                if (
                    last_main_domain != current_main_domain
                ):  # Condición 1: El dominio principal cambió
                    logger.warning(
                        f"El dominio principal del stream para {streamer_login} ha cambiado de '{last_main_domain}' a '{current_main_domain}'. Reiniciando grabación."
                    )
                    await self._handle_stream_online(streamer_login, current_stream_url)
                elif (  # Condición 2: El host es el mismo, pero la tarea de grabación falló
                    stream_info["recording_task"] is None
                    or stream_info["recording_task"].done()
                ):
                    logger.warning(
                        f"La tarea de grabación para {streamer_login} no está activa o ha terminado inesperadamente. Reiniciando."
                    )
                    await self._handle_stream_online(streamer_login, current_stream_url)
                else:  # Condición 3: Todo está bien, solo actualizamos la URL para la siguiente comprobación
                    stream_info["last_stream_url"] = current_stream_url

        except Exception as e:
            logger.error(
                f"Error en el monitoreo de {streamer_login}: {e}", exc_info=True
            )

    async def stop_monitoring(self):
        """Detiene el monitoreo y limpia las tareas activas."""
        logger.info("Deteniendo monitoreo de streams...")
        for streamer_login, info in list(self.active_streams.items()):
            if info["is_live"]:
                await self._handle_stream_offline(  # Esto cancelará la tarea y limpiará
                    streamer_login
                )
        logger.info("Monitoreo de streams detenido y recursos liberados.")
