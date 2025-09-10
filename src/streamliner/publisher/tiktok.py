# src/streamliner/publisher/tiktok.py

import httpx
from loguru import logger
from tenacity import retry, wait_fixed, stop_after_attempt
from pathlib import Path


class TikTokPublisher:
    """Gestiona la subida de videos a TikTok usando la Content Posting API."""

    BASE_URL = "https://open.tiktokapis.com/v2"

    def __init__(self, config, storage):
        self.config = config.publishing
        self.creds = config.credentials["tiktok"]  # Corregido para acceso a dict
        self.storage = storage
        self.client = httpx.AsyncClient(
            timeout=120
        )  # Aumentamos el timeout para subidas

    async def _get_headers(self):
        if not self.creds.access_token:
            raise ValueError("El token de acceso de TikTok no está configurado.")
        return {"Authorization": f"Bearer {self.creds.access_token}"}

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(5))
    async def upload_clip(
        self, video_path: str, streamer: str, dry_run: bool = False
    ) -> bool:
        """Sube un clip a TikTok."""
        if dry_run:
            logger.warning(
                f"[DRY-RUN] Simulación de subida del clip {video_path} a TikTok."
            )
            return True

        description = self.config.description_template.format(
            streamer_name=streamer, game_name="Gaming", clip_title="¡Momentazo!"
        )

        if self.config.upload_strategy == "PULL_FROM_URL":
            return await self._upload_by_url(video_path, description)
        elif self.config.upload_strategy == "MULTIPART":
            return await self._upload_by_multipart(video_path, description)
        else:
            logger.error(
                f"Estrategia de subida desconocida: {self.config.upload_strategy}"
            )
            return False

    async def _upload_by_url(self, file_key: str, description: str) -> bool:
        """Sube un video usando una URL pública (desde S3/R2)."""
        public_url = await self.storage.get_public_url(file_key)
        if not public_url:
            logger.error(
                f"No se pudo obtener una URL pública para {file_key}. No se puede subir."
            )
            return False

        logger.info(f"Iniciando subida a TikTok desde la URL: {public_url}")
        post_info = {
            "post_info": {"title": "Clip Épico", "description": description},
            "source_info": {"source": "PULL_FROM_URL", "video_url": public_url},
        }

        try:
            headers = await self._get_headers()
            url = f"{self.BASE_URL}/video/upload/"
            response = await self.client.post(url, headers=headers, json=post_info)
            response.raise_for_status()
            logger.success("✅ ¡Video subido a TikTok exitosamente desde URL!")
            return True
        except httpx.HTTPStatusError as e:
            logger.error(
                f"Error HTTP al subir a TikTok: {e.response.status_code} - {e.response.text}"
            )
            return False

    async def _upload_by_multipart(self, local_path: str, description: str) -> bool:
        """Sube un video local usando el método multipart."""
        logger.info(f"Iniciando subida a TikTok por multipart desde: {local_path}")
        if not Path(local_path).exists():
            logger.error(
                f"El archivo de video no existe en la ruta local: {local_path}"
            )
            return False

        try:
            # Paso 1: Pedir la URL de subida
            headers = await self._get_headers()
            url_init = f"{self.BASE_URL}/video/upload/"
            init_payload = {
                "post_info": {"title": "Clip Épico", "description": description},
                "source_info": {"source": "FILE_UPLOAD"},
            }
            response_init = await self.client.post(
                url_init, headers=headers, json=init_payload
            )
            response_init.raise_for_status()
            upload_url = response_init.json().get("data", {}).get("upload_url")
            if not upload_url:
                logger.error("No se recibió una URL de subida de TikTok.")
                return False

            # Paso 2: Subir el archivo de video a la URL recibida
            logger.info("URL de subida recibida. Subiendo el archivo de video...")
            with open(local_path, "rb") as video_file:
                files = {"video": (Path(local_path).name, video_file, "video/mp4")}
                # Usamos un cliente sin headers por defecto para la subida a S3/GCS de TikTok
                async with httpx.AsyncClient(timeout=300) as upload_client:
                    response_upload = await upload_client.put(
                        upload_url, files=files, headers={"Content-Type": "video/mp4"}
                    )
                    response_upload.raise_for_status()

            logger.success("✅ ¡Video subido a TikTok exitosamente por multipart!")
            return True
        except httpx.HTTPStatusError as e:
            logger.error(
                f"Error HTTP durante la subida multipart: {e.response.status_code} - {e.response.text}"
            )
            return False
        except Exception as e:
            logger.error(f"Error inesperado en la subida multipart: {e}")
            return False
