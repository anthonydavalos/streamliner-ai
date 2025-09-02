import asyncio
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential
from loguru import logger
from .config import AppConfig
from .downloader import Downloader
from .storage import get_storage

class Monitor:
    """Gestiona la monitorización de múltiples streamers de forma asíncrona."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.streamers = config.streamers
        self.storage = get_storage(config)
        self.client = httpx.AsyncClient(timeout=10)
        logger.info(f"Monitor configurado para los streamers: {self.streamers}")

    async def start(self):
        """Inicia una tarea de monitorización para cada streamer."""
        tasks = [self.monitor_streamer(streamer) for streamer in self.streamers]
        await asyncio.gather(*tasks)

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=60))
    async def get_streamer_status(self, streamer: str) -> dict:
        """Consulta la API (no oficial) de Kick para ver si un streamer está en vivo."""
        url = f"https://kick.com/api/v2/channels/{streamer}"
        try:
            response = await self.client.get(url)
            response.raise_for_status()
            data = response.json()
            if data.get("livestream"):
                logger.debug(f"Respuesta de API para {streamer} indica que está en vivo.")
                return {"is_live": True, "data": data}
            else:
                logger.debug(f"Respuesta de API para {streamer} indica que no está en vivo.")
                return {"is_live": False, "data": None}
        except httpx.HTTPStatusError as e:
            logger.error(f"Error HTTP al consultar el estado de {streamer}: {e.response.status_code}")
            raise
        except Exception as e:
            logger.error(f"Error inesperado al consultar el estado de {streamer}: {e}")
            raise

    async def monitor_streamer(self, streamer: str):
        """Ciclo de vida de la monitorización para un solo streamer."""
        logger.info(f"Iniciando monitorización para '{streamer}'.")
        while True:
            try:
                status = await self.get_streamer_status(streamer)
                if status["is_live"]:
                    logger.success(f"🟢 ¡{streamer} está EN VIVO! Iniciando descarga...")
                    downloader = Downloader(self.config, self.storage)
                    
                    # El downloader se encargará de descargar y luego disparar el procesamiento
                    await downloader.download_stream(streamer)
                    
                    logger.info(f"La sesión de {streamer} ha terminado. Reanudando monitoreo en {self.config.monitoring.reconnect_delay_seconds}s.")
                    await asyncio.sleep(self.config.monitoring.reconnect_delay_seconds)
                else:
                    logger.info(f"⚪ {streamer} no está en vivo. Próxima comprobación en {self.config.monitoring.check_interval_seconds}s.")
                    await asyncio.sleep(self.config.monitoring.check_interval_seconds)

            except Exception as e:
                logger.error(f"Fallo en el ciclo de monitorización de {streamer}: {e}. Reintentando en {self.config.monitoring.check_interval_seconds}s.")
                await asyncio.sleep(self.config.monitoring.check_interval_seconds)