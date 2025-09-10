# src/streamliner/monitor.py

import asyncio
import os
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential
from loguru import logger
import time
from urllib.parse import urlencode
import shutil
from pathlib import Path

from .config import AppConfig
from .downloader import Downloader
from .storage import get_storage


class Monitor:
    """
    Gestiona la monitorización de streamers y el apagado elegante del sistema.
    Versión final con lógica de limpieza y gestión de tareas centralizada.
    """

    def __init__(self, config: AppConfig):
        self.config = config
        self.streamers = list(set(config.streamers))  # Elimina duplicados
        self.storage = get_storage(config)

        self.client_id = os.getenv("KICK_CLIENT_ID")
        self.client_secret = os.getenv("KICK_CLIENT_SECRET")
        if not self.client_id or not self.client_secret:
            raise ValueError(
                "KICK_CLIENT_ID y KICK_CLIENT_SECRET deben estar en el .env"
            )

        self.client = httpx.AsyncClient(timeout=20)
        self.access_token = None
        self.token_expiry_time = 0

        self.active_download_tasks: dict[str, asyncio.Task] = {}

        logger.info(f"Monitor configurado para los streamers: {self.streamers}")

    async def _get_app_access_token(self):
        token_url = "https://id.kick.com/oauth/token"
        payload = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        logger.info("Solicitando nuevo App Access Token de Kick...")
        try:
            response = await self.client.post(token_url, data=payload)
            response.raise_for_status()
            token_data = response.json()
            self.access_token = token_data.get("access_token")
            if not self.access_token:
                raise ValueError("Respuesta sin 'access_token'")
            self.token_expiry_time = (
                time.time() + token_data.get("expires_in", 3600) - 60
            )
            logger.success("App Access Token obtenido exitosamente.")
        except httpx.HTTPStatusError as e:
            logger.error(
                f"No se pudo obtener el token: {e.response.status_code} - {e.response.text}"
            )
            raise

    async def _ensure_token_is_valid(self):
        if not self.access_token or time.time() >= self.token_expiry_time:
            await self._get_app_access_token()

    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=5, max=60)
    )
    async def get_streamers_status(self) -> dict:
        await self._ensure_token_is_valid()
        query_params = [("slug", streamer) for streamer in self.streamers]
        url = f"https://api.kick.com/public/v1/channels?{urlencode(query_params)}"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
        }

        try:
            logger.debug(f"Consultando estado para: {self.streamers}")
            response = await self.client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()

            live_statuses = {slug: False for slug in self.streamers}
            results_data = data.get("data", [])
            if not isinstance(results_data, list):
                logger.warning(
                    f"La respuesta de la API no fue una lista, fue: {results_data}"
                )
                results_data = []
            for channel_info in results_data:
                slug = channel_info.get("slug")
                if slug in live_statuses:
                    is_live = channel_info.get("stream") and channel_info["stream"].get(
                        "is_live"
                    )
                    live_statuses[slug] = is_live
            return live_statuses
        except httpx.HTTPStatusError as e:
            logger.error(f"Error HTTP al consultar canales: {e.response.status_code}")
            raise
        except Exception as e:
            logger.error(f"Error inesperado al consultar canales: {e}")
            raise

    async def _run_monitoring_cycle(self):
        """El ciclo principal que gestiona el lanzamiento y la limpieza de tareas."""
        while True:
            try:
                logger.info("Iniciando nuevo ciclo de monitorización...")
                live_statuses = await self.get_streamers_status()

                for streamer, is_live in live_statuses.items():
                    is_downloading = streamer in self.active_download_tasks

                    if is_live and not is_downloading:
                        logger.success(
                            f"🟢 {streamer} está EN VIVO. Lanzando tarea de descarga..."
                        )
                        downloader = Downloader(self.config, self.storage)
                        task = asyncio.create_task(downloader.download_stream(streamer))
                        self.active_download_tasks[streamer] = task

                    elif not is_live:
                        logger.info(f"⚪ {streamer} no está en vivo.")

                # Limpiamos las tareas que ya han terminado de forma natural
                done_tasks = []
                for streamer, task in self.active_download_tasks.items():
                    if task.done():
                        logger.info(
                            f"Tarea de descarga para {streamer} ha finalizado de forma natural."
                        )
                        if task.exception():
                            logger.error(
                                f"La tarea de {streamer} terminó con un error: {task.exception()}"
                            )
                        done_tasks.append(streamer)

                # Borramos la carpeta de sesión DESPUÉS de que la tarea haya terminado
                for streamer in done_tasks:
                    # La ruta de la carpeta se infiere, ya que el downloader es predecible
                    session_dir_path_str = self.config.real_time_processing.get(
                        "chunk_storage_path", "data/chunks"
                    )
                    # Buscamos la carpeta específica de la sesión
                    for path in Path(session_dir_path_str).iterdir():
                        if path.is_dir() and streamer in path.name:
                            logger.info(
                                f"Limpiando directorio de sesión para {streamer}: {path.name}"
                            )
                            shutil.rmtree(path)
                    del self.active_download_tasks[streamer]

            except Exception as e:
                logger.error(f"Fallo en el ciclo de monitorización: {e}.")

            logger.info(
                f"Ciclo completado. Esperando {self.config.monitoring.check_interval_seconds} segundos..."
            )
            await asyncio.sleep(self.config.monitoring.check_interval_seconds)

    async def start(self):
        """Inicia el monitor."""
        await self._ensure_token_is_valid()
        await self._run_monitoring_cycle()

    async def shutdown(self):
        """Cancela tareas pendientes y limpia los recursos antes de salir."""
        logger.info("Iniciando apagado elegante...")

        if self.active_download_tasks:
            logger.info(
                f"Cancelando {len(self.active_download_tasks)} tareas de descarga activas..."
            )
            for task in self.active_download_tasks.values():
                task.cancel()
            await asyncio.gather(
                *self.active_download_tasks.values(), return_exceptions=True
            )

        chunk_root_path = Path(self.config.real_time_processing.chunk_storage_path)
        if chunk_root_path.exists():
            logger.info(
                "Limpiando directorios de chunks restantes por apagado manual..."
            )
            for session_dir in chunk_root_path.iterdir():
                if session_dir.is_dir():
                    try:
                        shutil.rmtree(session_dir)
                        logger.success(
                            f"Directorio de sesión {session_dir.name} limpiado."
                        )
                    except OSError as e:
                        logger.error(
                            f"No se pudo limpiar el directorio de sesión {session_dir.name}: {e}"
                        )

        await self.client.aclose()
        logger.success("Apagado elegante completado.")
