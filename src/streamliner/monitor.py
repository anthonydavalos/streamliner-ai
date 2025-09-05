# src/streamliner/monitor.py

import asyncio
import os
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential
from loguru import logger
import time
from urllib.parse import urlencode

from .config import AppConfig
from .downloader import Downloader
from .storage import get_storage


class Monitor:
    """
    Gestiona la monitorización de streamers usando el flujo de autenticación oficial
    de Kick y el endpoint de consulta por lotes, basado en la investigación
    exitosa con Apps Script.
    """

    def __init__(self, config: AppConfig):
        self.config = config
        self.streamers = config.streamers
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
        self.currently_downloading = (
            set()
        )  # Para no descargar dos veces al mismo streamer

        logger.info(f"Monitor configurado para los streamers: {self.streamers}")

    async def _get_app_access_token(self):
        """Obtiene un App Access Token de la API de Kick."""
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
                raise ValueError("La respuesta no contenía 'access_token'")

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
        """Verifica y refresca el token si es necesario."""
        if not self.access_token or time.time() >= self.token_expiry_time:
            await self._get_app_access_token()

    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=5, max=60)
    )
    async def get_streamers_status(self) -> dict:
        """Consulta el estado de TODOS los streamers en una sola llamada a la API."""
        await self._ensure_token_is_valid()

        # Construye la URL con múltiples parámetros 'slug', como en Apps Script
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

            # Procesamos la respuesta para devolver un diccionario de estados
            live_statuses = {slug: False for slug in self.streamers}
            results_data = data.get("data", [])
            if not isinstance(results_data, list):
                logger.warning(
                    f"La respuesta de la API no fue una lista: {results_data}"
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

    async def _handle_download(self, streamer):
        """Maneja la descarga para un streamer que está en vivo."""
        logger.info(f"Iniciando proceso de descarga para {streamer}...")
        self.currently_downloading.add(streamer)
        try:
            downloader = Downloader(self.config, self.storage)
            await downloader.download_stream(streamer)
        finally:
            logger.info(f"Proceso de descarga para {streamer} finalizado.")
            self.currently_downloading.remove(streamer)

    async def _run_monitoring_cycle(self):
        """El ciclo principal que se ejecuta cada X segundos."""
        while True:
            try:
                logger.info("Iniciando nuevo ciclo de monitorización...")
                live_statuses = await self.get_streamers_status()

                for streamer, is_live in live_statuses.items():
                    if is_live and streamer not in self.currently_downloading:
                        logger.success(
                            f"🟢 {streamer} está EN VIVO. Lanzando tarea de descarga..."
                        )
                        # Lanza la descarga en segundo plano para no bloquear el monitor
                        asyncio.create_task(self._handle_download(streamer))
                    elif not is_live:
                        logger.info(f"⚪ {streamer} no está en vivo.")

            except Exception as e:
                logger.error(
                    f"Fallo en el ciclo de monitorización: {e}. Reintentando en el próximo ciclo."
                )

            logger.info(
                f"Ciclo de monitorización completado. Esperando {self.config.monitoring.check_interval_seconds} segundos..."
            )
            await asyncio.sleep(self.config.monitoring.check_interval_seconds)

    async def start(self):
        """Inicia el monitor."""
        await self._ensure_token_is_valid()
        await self._run_monitoring_cycle()
        await self.client.aclose()
