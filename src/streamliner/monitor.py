# src/streamliner/monitor.py

import asyncio
from playwright.async_api import async_playwright, BrowserContext
from tenacity import retry, stop_after_attempt, wait_exponential
from loguru import logger

from .config import AppConfig
from .downloader import Downloader
from .storage import get_storage


class Monitor:
    """
    Gestiona la monitorización de streamers usando Playwright.
    Versión final: No usa la API, sino que "ve" la página del canal para detectar
    el indicador de "EN VIVO", evitando las protecciones de API más fuertes.
    """

    def __init__(self, config: AppConfig):
        self.config = config
        self.streamers = config.streamers
        self.storage = get_storage(config)
        logger.info(f"Monitor configurado para los streamers: {self.streamers}")

    async def start(self):
        """Inicia Playwright y lanza las tareas de monitorización."""
        user_data_dir = "./playwright_user_data"

        async with async_playwright() as p:
            browser_context = await p.chromium.launch_persistent_context(
                user_data_dir,
                headless=False,
            )
            logger.info(
                f"Navegador Playwright (Chromium) iniciado con perfil dedicado en: {user_data_dir}"
            )

            tasks = [
                self.monitor_streamer(streamer, browser_context)
                for streamer in self.streamers
            ]
            await asyncio.gather(*tasks)

            await browser_context.close()

    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=5, max=60)
    )
    async def get_streamer_status(
        self, streamer: str, browser_context: BrowserContext
    ) -> dict:
        """
        Verifica si un streamer está en vivo navegando a su página y buscando
        un elemento visual que indique el estado "EN VIVO".
        """
        channel_url = f"https://kick.com/{streamer}"
        page = None
        try:
            page = await browser_context.new_page()
            logger.debug(
                f"Navegando a la página de {streamer} para comprobar estado visual..."
            )

            # Navegamos y esperamos a que la red esté mayormente inactiva, señal de que cargó.
            await page.goto(channel_url, timeout=30000, wait_until="networkidle")

            # Selector CSS que busca un elemento que solo suele existir cuando el stream está en vivo.
            # Este es un selector común para el indicador rojo de "LIVE".
            live_indicator_selector = "div.live-badge-container"

            # Contamos cuántos de estos elementos hay en la página.
            live_indicator_count = await page.locator(live_indicator_selector).count()

            if live_indicator_count > 0:
                logger.success(
                    f"Indicador visual de 'EN VIVO' encontrado para {streamer}."
                )
                return {"is_live": True}
            else:
                logger.info(
                    f"⚪ {streamer} no está en vivo (no se encontró indicador visual)."
                )
                return {"is_live": False}
        except Exception as e:
            logger.error(f"Error al comprobar el estado visual de {streamer}: {e}")
            raise
        finally:
            if page:
                await page.close()

    async def monitor_streamer(self, streamer: str, browser_context: BrowserContext):
        """Ciclo de vida de la monitorización para un solo streamer."""
        logger.info(f"Iniciando monitorización para '{streamer}'.")
        while True:
            try:
                status = await self.get_streamer_status(streamer, browser_context)
                if status["is_live"]:
                    logger.success(
                        f"🟢 ¡{streamer} está EN VIVO! Iniciando descarga..."
                    )
                    downloader = Downloader(self.config, self.storage)
                    await downloader.download_stream(streamer)
                    logger.info(
                        f"La sesión de {streamer} ha terminado. Reanudando monitoreo en {self.config.monitoring.reconnect_delay_seconds}s."
                    )
                    await asyncio.sleep(self.config.monitoring.reconnect_delay_seconds)
                else:
                    # El log de 'no en vivo' ya se muestra en get_streamer_status
                    await asyncio.sleep(self.config.monitoring.check_interval_seconds)

            except Exception:
                # El error ya se loguea en get_streamer_status, y tenacity se encarga de reintentar.
                # Si los reintentos fallan, el error se propaga aquí.
                logger.error(
                    f"Fallo persistente en la monitorización de {streamer} tras varios reintentos. Esperando antes de volver a empezar el ciclo."
                )
                await asyncio.sleep(
                    self.config.monitoring.check_interval_seconds * 2
                )  # Espera más larga tras fallo persistente
