# src/streamliner/monitor.py

import asyncio
from playwright.async_api import async_playwright, Playwright, Browser
from tenacity import retry, stop_after_attempt, wait_exponential
from loguru import logger

from .config import AppConfig
from .downloader import Downloader
from .storage import get_storage

class Monitor:
    """Gestiona la monitorización de múltiples streamers usando Playwright para evitar el bloqueo."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.streamers = config.streamers
        self.storage = get_storage(config)
        logger.info(f"Monitor configurado para los streamers: {self.streamers}")

    async def start(self):
        """Inicia Playwright y lanza las tareas de monitorización."""
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            logger.info("Navegador Playwright (Chromium) iniciado en modo headless.")
            
            tasks = [self.monitor_streamer(streamer, browser) for streamer in self.streamers]
            await asyncio.gather(*tasks)
            
            await browser.close()

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=60))
    async def get_streamer_status(self, streamer: str, browser: Browser) -> dict:
        """Consulta la API de Kick usando una instancia de navegador real."""
        url = f"https://kick.com/api/v2/channels/{streamer}"
        page = None
        try:
            page = await browser.new_page()
            response = await page.goto(url, timeout=15000)
            
            if not response.ok:
                logger.error(f"Error HTTP al consultar el estado de {streamer}: {response.status}")
                # Forzamos un reintento si el status no es OK.
                raise IOError(f"HTTP status {response.status}")

            data = await response.json()
            
            if data.get("livestream"):
                logger.debug(f"Respuesta de API para {streamer} indica que está en vivo.")
                return {"is_live": True, "data": data}
            else:
                logger.debug(f"Respuesta de API para {streamer} indica que no está en vivo.")
                return {"is_live": False, "data": None}
        except Exception as e:
            logger.error(f"Error inesperado al consultar el estado de {streamer}: {e}")
            raise
        finally:
            if page:
                await page.close()

    async def monitor_streamer(self, streamer: str, browser: Browser):
        """Ciclo de vida de la monitorización para un solo streamer."""
        logger.info(f"Iniciando monitorización para '{streamer}'.")
        while True:
            try:
                status = await self.get_streamer_status(streamer, browser)
                if status["is_live"]:
                    logger.success(f"🟢 ¡{streamer} está EN VIVO! Iniciando descarga...")
                    downloader = Downloader(self.config, self.storage)
                    await downloader.download_stream(streamer)
                    logger.info(f"La sesión de {streamer} ha terminado. Reanudando monitoreo en {self.config.monitoring.reconnect_delay_seconds}s.")
                    await asyncio.sleep(self.config.monitoring.reconnect_delay_seconds)
                else:
                    logger.info(f"⚪ {streamer} no está en vivo. Próxima comprobación en {self.config.monitoring.check_interval_seconds}s.")
                    await asyncio.sleep(self.config.monitoring.check_interval_seconds)

            except Exception as e:
                logger.error(f"Fallo en el ciclo de monitorización de {streamer}: {e}. Reintentando...")
                # La lógica de reintento ya está en get_streamer_status, 
                # aquí solo esperamos antes de volver a empezar el ciclo.
                await asyncio.sleep(self.config.monitoring.check_interval_seconds)