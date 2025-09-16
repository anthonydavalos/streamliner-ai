# src/streamliner/publisher/tiktok.py

import httpx
from loguru import logger
from tenacity import retry, wait_fixed, stop_after_attempt
import os
import json
import time  # Importar para manejar timestamps
import asyncio  # Necesario para asyncio.sleep en el lock
import math  # Para math.ceil en el cálculo de chunks


class TikTokPublisher:
    """
    Gestiona la subida de vídeos a TikTok utilizando la Content Posting API.
    Implementación asíncrona basada en la investigación de la documentación oficial.
    """

    PROD_URL = "https://open.tiktokapis.com/v2"
    SANDBOX_URL = "https://open-api.tiktok.com/v2"

    MIN_CHUNK_SIZE = 5 * 1024 * 1024
    MAX_CHUNK_SIZE = 64 * 1024 * 1024

    def __init__(self, config, storage):  # <-- config es el objeto AppConfig completo
        self.config = config.publishing  # Configuración específica de publicación
        self.creds = config.credentials["tiktok"]  # Credenciales de TikTok
        self.storage = storage

        if self.creds.environment == "sandbox":
            self.base_url = self.SANDBOX_URL
            logger.warning("El publicador de TikTok está operando en MODO SANDBOX.")
        else:
            self.base_url = self.PROD_URL

        self.client = httpx.AsyncClient(timeout=120)

        self._access_token = self.creds.access_token
        self._refresh_token = self.creds.refresh_token
        self._token_expires_at = 0
        self._refresh_token_lock = False

    async def _get_valid_access_token(self) -> str | None:
        """
        Devuelve un access_token válido, refrescándolo si es necesario.
        """
        if not self._access_token or (
            self._token_expires_at - time.time() < 300
        ):  # 300 segundos = 5 minutos
            logger.info(
                "El Access Token de TikTok está a punto de expirar o no está inicializado. Intentando refrescar..."
            )
            await self._refresh_access_token()

        return self._access_token

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(5))
    async def _refresh_access_token(self):
        """
        Refresca el access_token usando el refresh_token.
        """
        if self._refresh_token_lock:
            logger.info(
                "Ya hay una operación de refresco de token en progreso. Esperando..."
            )
            while self._refresh_token_lock:
                await asyncio.sleep(1)
            return

        self._refresh_token_lock = True
        REFRESH_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
        payload = {
            "client_key": self.creds.client_key,
            "client_secret": self.creds.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
        }

        try:
            logger.info("Enviando solicitud para refrescar el token de acceso...")
            response = await self.client.post(REFRESH_TOKEN_URL, data=payload)
            response.raise_for_status()

            token_data = response.json()

            new_access_token = token_data.get("access_token")
            new_refresh_token = token_data.get("refresh_token")
            expires_in = token_data.get("expires_in")

            if new_access_token:
                self._access_token = new_access_token
                self._token_expires_at = time.time() + expires_in
                if new_refresh_token:
                    self._refresh_token = new_refresh_token

                logger.success(
                    f"Access Token de TikTok refrescado con éxito. Expira en {expires_in} segundos."
                )
                logger.debug(
                    f"Nuevo Access Token (últimos 4): ...{self._access_token[-4:]}"
                )
            else:
                logger.error(
                    "No se recibió un nuevo access_token en la respuesta de refresco."
                )
                self._access_token = None
        except httpx.HTTPStatusError as e:
            logger.error(
                f"Error HTTP al refrescar el token: {e.response.status_code} - {e.response.text}"
            )
            self._access_token = None
            raise
        except Exception as e:
            logger.error(f"Error inesperado al refrescar el token: {e}")
            self._access_token = None
            raise
        finally:
            self._refresh_token_lock = False

    async def _perform_chunked_upload(
        self, upload_url: str, video_path: str, file_size: int, chunk_size: int
    ):
        """Método auxiliar para realizar la subida por fragmentos."""
        async with httpx.AsyncClient(timeout=300) as upload_client:
            with open(video_path, "rb") as f:
                bytes_sent = 0
                while bytes_sent < file_size:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break

                    chunk_len = len(chunk)
                    start_byte = bytes_sent
                    end_byte = bytes_sent + chunk_len - 1

                    upload_headers = {
                        "Content-Type": "video/mp4",
                        "Content-Range": f"bytes {start_byte}-{end_byte}/{file_size}",
                    }
                    logger.debug(f"Subiendo chunk: {upload_headers['Content-Range']}")

                    try:
                        response = await upload_client.put(
                            upload_url, headers=upload_headers, content=chunk
                        )
                        response.raise_for_status()
                        bytes_sent += chunk_len
                    except httpx.HTTPStatusError as e:
                        logger.error(
                            f"Error al subir el chunk: {e.response.status_code} - {e.response.text}"
                        )
                        return False

        logger.success("Todos los chunks han sido subidos con éxito.")
        return True

    async def _initialize_upload(self, init_url: str, payload: dict) -> dict | None:
        """Método auxiliar para inicializar una subida."""
        access_token = await self._get_valid_access_token()
        if not access_token:
            logger.error(
                "No se pudo obtener un Access Token válido para inicializar la subida."
            )
            return None

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        }
        try:
            logger.info(f"Enviando solicitud de inicialización a {init_url}...")
            response = await self.client.post(
                init_url, headers=headers, data=json.dumps(payload)
            )
            response.raise_for_status()
            result = response.json()

            if result.get("error", {}).get("code") != "ok":
                logger.error(
                    f"Error en la respuesta de inicialización: {result['error']}"
                )
                return None

            logger.success("Inicialización exitosa.")
            return result.get("data")
        except httpx.HTTPStatusError as e:
            logger.error(
                f"Error en la solicitud de inicialización: {e.response.status_code} - {e.response.text}"
            )
            return None

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(5))
    async def upload_clip(
        self, file_or_url: str, streamer: str, dry_run: bool = False
    ) -> bool:
        # *** AJUSTE CLAVE AQUÍ para dry_run ***
        if dry_run:
            logger.info(
                f"Modo Dry-Run activado para TikTok. "
                f"No se subirá el clip '{file_or_url}'."
            )
            return True  # Simula éxito en dry-run para no bloquear el pipeline

        if not self.creds.access_token or not self.creds.open_id:
            logger.error(
                "El Access Token o el Open ID de TikTok no están configurados en la configuración."
            )
            return False

        if self.creds.environment == "sandbox":
            logger.info(f"Enviando clip de prueba al SANDBOX de TikTok: {file_or_url}")
            # En Sandbox, solo podemos subir como borrador (direct_post=False)
            return await self.upload_video(file_or_url, direct_post=False)
        else:  # Entorno de Producción
            post_details = {
                "title": self.config.description_template.format(
                    streamer_name=streamer, game_name="Gaming", clip_title="¡Momentazo!"
                ),
                "privacy_level": "SELF_ONLY",  # O el nivel de privacidad que desees en producción
            }
            return await self.upload_video(
                file_or_url, direct_post=True, **post_details
            )

    async def upload_video(
        self, video_path: str, direct_post: bool = False, **post_info
    ) -> str | None:
        if not os.path.exists(video_path):
            logger.error(f"Error: El archivo no existe en {video_path}")
            return None

        if not await self._get_valid_access_token():
            logger.error(
                "No se pudo obtener un Access Token válido para la subida de video."
            )
            return None

        file_size = os.path.getsize(video_path)

        standard_chunk_size = 20 * 1024 * 1024  # 20 MB

        if file_size <= standard_chunk_size:
            chunk_size = file_size
            total_chunks = 1
        else:
            chunk_size = standard_chunk_size
            total_chunks = math.ceil(file_size / chunk_size)

        if direct_post:
            logger.info("Intentando publicación directa...")
            init_url = f"{self.base_url}/post/publish/video/init/"
            payload = {
                "post_info": post_info,
                "source_info": {"source": "FILE_UPLOAD", "video_size": file_size},
            }
        else:
            logger.info("Intentando subir como borrador a la bandeja de entrada...")
            init_url = f"{self.base_url}/post/publish/inbox/video/init/"
            payload = {
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": file_size,
                    "chunk_size": chunk_size,
                    "total_chunk_count": total_chunks,
                }
            }

        init_data = await self._initialize_upload(init_url, payload)
        if not init_data or "upload_url" not in init_data:
            return None

        upload_url = init_data["upload_url"]
        publish_id = init_data["publish_id"]

        upload_success = await self._perform_chunked_upload(
            upload_url, video_path, file_size, chunk_size
        )
        if upload_success:
            logger.success(
                f"Video subido a TikTok (Sandbox). Publish ID: {publish_id}. El estado final no se puede verificar a través de la API en Sandbox."
            )
            return publish_id
        else:
            logger.error("La subida del video falló.")
            return None
