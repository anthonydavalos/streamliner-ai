import httpx
from loguru import logger
from tenacity import retry, wait_fixed, stop_after_attempt

class TikTokPublisher:
    """
    Gestiona la subida de videos a TikTok usando la Content Posting API.
    
    IMPORTANTE: La automatización completa con esta API requiere aprobación
    y permisos especiales de TikTok. Este código implementa el flujo
    documentado, pero su éxito depende de que las credenciales tengan
    el scope 'video.upload'.
    """
    BASE_URL = "https://open.tiktokapis.com/v2"

    def __init__(self, config, storage):
        self.config = config.publishing
        self.creds = config.credentials['tiktok']
        self.storage = storage
        self.client = httpx.AsyncClient()

    async def _get_headers(self):
        # En una implementación real, aquí se manejaría el refresh token si es necesario
        if not self.creds.access_token:
            raise ValueError("El token de acceso de TikTok no está configurado.")
        return {
            "Authorization": f"Bearer {self.creds.access_token}",
            "Content-Type": "application/json",
        }

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(5))
    async def upload_clip(self, video_path: str, streamer: str, dry_run: bool = False) -> bool:
        """Sube un clip a TikTok."""
        if dry_run:
            logger.warning(f"[DRY-RUN] Simulación de subida del clip {video_path} a TikTok.")
            return True

        description = self.config.description_template.format(
            streamer_name=streamer, game_name="Gaming", clip_title="¡Momentazo!"
        )

        if self.config.upload_strategy == "PULL_FROM_URL":
            return await self._upload_by_url(video_path, description)
        else:
            # El método MULTIPART es más complejo y no se implementa aquí
            # por brevedad, pero seguiría un flujo similar de dos pasos.
            logger.error("La estrategia MULTIPART no está implementada. Use PULL_FROM_URL con S3/R2.")
            return False

    async def _upload_by_url(self, file_key: str, description: str) -> bool:
        """Sube un video usando una URL pública (desde S3/R2)."""
        public_url = await self.storage.get_public_url(file_key)
        if not public_url:
            logger.error(f"No se pudo obtener una URL pública para {file_key}. No se puede subir.")
            return False
            
        logger.info(f"Iniciando subida a TikTok desde la URL: {public_url}")
        
        post_info = {
            "post_info": {
                "title": "Clip Épico de Kick",
                "description": description,
                "privacy_level": "PUBLIC_TO_EVERYONE",
                "disable_comment": False,
                "disable_duet": False,
                "disable_stitch": False,
            },
            "source_info": {
                "source": "PULL_FROM_URL",
                "video_url": public_url,
            }
        }
        
        try:
            headers = await self._get_headers()
            url = f"{self.BASE_URL}/video/upload/?open_id={self.creds.open_id}"
            
            response = await self.client.post(url, headers=headers, json=post_info)
            response.raise_for_status()
            
            data = response.json()
            if data.get("error", {}).get("code") != "ok":
                logger.error(f"Error en la API de TikTok: {data['error']}")
                return False

            logger.success("✅ ¡Video subido a TikTok exitosamente!")
            return True

        except httpx.HTTPStatusError as e:
            logger.error(f"Error HTTP al subir a TikTok: {e.response.status_code} - {e.response.text}")
            return False
        except Exception as e:
            logger.error(f"Error inesperado durante la subida a TikTok: {e}")
            return False