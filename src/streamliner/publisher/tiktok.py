# src/streamliner/publisher/tiktok.py

import httpx
from loguru import logger
from tenacity import (
    retry,
    wait_exponential,
    stop_after_attempt,
    retry_if_exception_type,
)
import os
import json
import time  # Importar para manejar timestamps
import asyncio  # Necesario para asyncio.sleep en el lock
import math  # Para math.ceil en el cálculo de chunks
from pathlib import Path


class TikTokPublisher:
    """
    Gestiona la subida de vídeos a TikTok utilizando la Content Posting API.
    Implementación asíncrona basada en la investigación de la documentación oficial.
    """

    PROD_URL = "https://open.tiktokapis.com/v2"
    SANDBOX_URL = "https://open-api.tiktok.com/v2"

    MIN_CHUNK_SIZE = 5 * 1024 * 1024
    MAX_CHUNK_SIZE = 64 * 1024 * 1024

    def __init__(
        self, app_config, storage
    ):  # app_config es el objeto AppConfig completo
        self.app_config = app_config  # Opcional, pero bueno para depurar
        self.config = app_config.publishing

        # Acceder a las credenciales desde el objeto TikTokCredentials ya cargado
        self.creds = app_config.credentials["tiktok"]  # Esto ahora FUNCIONARÁ
        self.storage = storage

        if self.creds.environment == "sandbox":  # Usar self.creds.environment
            self.base_url = self.SANDBOX_URL
            logger.warning("El publicador de TikTok está operando en MODO SANDBOX.")
        else:
            self.base_url = self.PROD_URL

        # Timeout más generoso: 30s para conectar, 5 minutos para leer/escribir
        timeout_config = httpx.Timeout(30.0, connect=60.0, read=300.0, write=300.0)
        self.client = httpx.AsyncClient(timeout=timeout_config)

        self._access_token = self.creds.access_token  # Esto ya estará cargado
        self._refresh_token = self.creds.refresh_token  # Esto ya estará cargado
        self._token_expires_at = 0
        # --- MEJORA: Usar un asyncio.Lock real para evitar race conditions ---
        self._refresh_token_lock = asyncio.Lock()
        # Estado persistente de sandbox (ruta en data_dir)
        try:
            data_dir = Path(self.app_config.paths.data_dir)
        except Exception:
            data_dir = Path("data")
        self._sandbox_state_file = data_dir / ".tiktok_sandbox_state.json"

        # Timestamp del último spam_risk para aplicar backoff en sandbox
        self._last_sandbox_spam_risk_ts = 0.0
        # Flag: endpoint BYTES no disponible (403/404) detectado previamente
        self._sandbox_bytes_unavailable = False
        self._load_sandbox_state()
        # Leer scopes del entorno (si están disponibles) para decidir estrategias válidas
        scopes_env = os.getenv("TIKTOK_SCOPES", "") or ""
        self._scopes = {
            s.strip() for s in scopes_env.replace(" ", "").split(",") if s.strip()
        }

    def _has_scope(self, scope: str) -> bool:
        return scope in self._scopes

    def _load_sandbox_state(self) -> None:
        try:
            if self._sandbox_state_file.exists():
                data = json.loads(self._sandbox_state_file.read_text(encoding="utf-8"))
                ts = data.get("last_spam_risk_ts")
                if isinstance(ts, (int, float)):
                    self._last_sandbox_spam_risk_ts = float(ts)
                bytes_unavail = data.get("bytes_unavailable")
                if isinstance(bytes_unavail, bool):
                    self._sandbox_bytes_unavailable = bytes_unavail
        except Exception as e:
            logger.debug(f"No se pudo cargar el estado de sandbox: {e}")

    def _save_sandbox_state(self) -> None:
        try:
            self._sandbox_state_file.parent.mkdir(parents=True, exist_ok=True)
            state = {
                "last_spam_risk_ts": self._last_sandbox_spam_risk_ts,
                "bytes_unavailable": self._sandbox_bytes_unavailable,
            }
            self._sandbox_state_file.write_text(json.dumps(state), encoding="utf-8")
        except Exception as e:
            logger.debug(f"No se pudo guardar el estado de sandbox: {e}")

    # Y en los métodos de TikTokPublisher (_refresh_access_token y upload_clip),
    # sigues usando self.creds.client_key, self.creds.open_id, etc.
    # ¡Porque ahora self.creds es un objeto TikTokCredentials válido!

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

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.RequestError, httpx.HTTPStatusError)),
    )
    async def _refresh_access_token(self):
        """
        Refresca el access_token usando el refresh_token.
        Usa un lock para prevenir múltiples refrescos simultáneos.
        """
        REFRESH_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
        payload = {
            "client_key": self.creds.client_key,
            "client_secret": self.creds.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,  # Este ya es correcto
        }

        try:
            async with self._refresh_token_lock:
                # Volver a comprobar si el token ya fue refrescado mientras se esperaba el lock
                if self._token_expires_at - time.time() > 300:
                    logger.info("El token ya fue refrescado por otra tarea. Omitiendo.")
                    return

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

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(httpx.RequestError),
    )
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
        if bytes_sent := file_size % chunk_size:
            logger.debug(
                f"Validación post-subida: file_size%chunk_size={bytes_sent}. Último chunk parcial esperado."
            )
        return True

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.RequestError, httpx.HTTPStatusError)),
    )
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
            # Detectar explícitamente spam_risk en SANDBOX para aplicar backoff
            body = None
            try:
                body = e.response.json()
            except Exception:
                body = None
            if (
                body
                and body.get("error", {}).get("code")
                == "spam_risk_too_many_pending_share"
            ):
                self._last_sandbox_spam_risk_ts = time.time()
                self._save_sandbox_state()
                logger.error(
                    f"Error en la solicitud de inicialización: {e.response.status_code} - {e.response.text}"
                )
                logger.warning(
                    "Detectado spam_risk_too_many_pending_share. Se activará backoff para MULTIPART en sandbox."
                )
                return None
            logger.error(
                f"Error en la solicitud de inicialización: {e.response.status_code} - {e.response.text}"
            )
            return None

    async def upload_clip(
        self, file_or_url: str, streamer: str, dry_run: bool = False
    ) -> bool:
        # Dry-run: no subir realmente
        if dry_run:
            logger.info(
                f"Modo Dry-Run activado para TikTok. No se subirá el clip '{file_or_url}'."
            )
            return True

        # Cooldown para evitar rate limiting
        cooldown_seconds = self.config.upload_cooldown_seconds
        if cooldown_seconds > 0:
            logger.info(
                f"Esperando {cooldown_seconds} segundos antes de la subida a TikTok (cooldown)..."
            )
            await asyncio.sleep(cooldown_seconds)

        # Validaciones básicas
        if not self._access_token or not self.creds.open_id:
            logger.error(
                "El Access Token o el Open ID de TikTok no están configurados."
            )
            return False

        if self.creds.environment == "sandbox":
            strategy = (self.config.upload_strategy or "").upper()
            logger.info(
                f"Enviando clip de prueba al SANDBOX de TikTok: {file_or_url} (estrategia={strategy})"
            )

            if strategy == "MULTIPART":
                backoff_sec = getattr(self.config, "sandbox_spam_backoff_seconds", 900)
                can_try_multipart = (
                    not self._last_sandbox_spam_risk_ts
                    or (time.time() - self._last_sandbox_spam_risk_ts) >= backoff_sec
                )
                if can_try_multipart:
                    publish_id = await self.upload_video(file_or_url, direct_post=False)
                    if publish_id:
                        return True
                else:
                    remaining = int(
                        backoff_sec - (time.time() - self._last_sandbox_spam_risk_ts)
                    )
                    logger.warning(
                        f"Omitiendo MULTIPART por backoff de spam_risk. Reintenta en ~{remaining}s."
                    )

                if (
                    getattr(self.config, "sandbox_allow_bytes_upload", False)
                    and not self._sandbox_bytes_unavailable
                ):
                    bytes_ok = await self.upload_video_bytes_for_sandbox(
                        file_or_url, allow_multipart_fallback=False
                    )
                    if bytes_ok:
                        return True

                # DIRECT_POST requiere 'video.publish'; omitir si el token no lo tiene
                if getattr(
                    self.config, "sandbox_allow_direct_post", False
                ) and self._has_scope("video.publish"):
                    logger.warning(
                        "Subida directa por bytes falló o está deshabilitada. Intentando publicación directa (requiere scope 'video.publish')."
                    )
                    post_details = {
                        "title": self.config.description_template.format(
                            streamer_name=streamer,
                            game_name="Gaming",
                            clip_title="¡Momentazo!",
                        ),
                        "privacy_level": "SELF_ONLY",
                    }
                    publish_id = await self.upload_video(
                        file_or_url, direct_post=True, **post_details
                    )
                    return bool(publish_id)
                else:
                    logger.info(
                        "DIRECT_POST no será usado en SANDBOX (deshabilitado o sin scope 'video.publish'). Terminando con fallo tras MULTIPART/BYTES."
                    )
                    return False

            elif strategy == "DIRECT_POST":
                if not getattr(
                    self.config, "sandbox_allow_direct_post", False
                ) or not self._has_scope("video.publish"):
                    logger.info(
                        "DIRECT_POST deshabilitado o sin scope en SANDBOX. Probando MULTIPART (inbox) y BYTES si aplica."
                    )
                    backoff_sec = getattr(
                        self.config, "sandbox_spam_backoff_seconds", 900
                    )
                    can_try_multipart = (
                        not self._last_sandbox_spam_risk_ts
                        or (time.time() - self._last_sandbox_spam_risk_ts)
                        >= backoff_sec
                    )
                    if can_try_multipart:
                        publish_id = await self.upload_video(
                            file_or_url, direct_post=False
                        )
                        if publish_id:
                            return True
                    else:
                        remaining = int(
                            backoff_sec
                            - (time.time() - self._last_sandbox_spam_risk_ts)
                        )
                        logger.warning(
                            f"Omitiendo MULTIPART por backoff de spam_risk. Reintenta en ~{remaining}s."
                        )
                    if (
                        getattr(self.config, "sandbox_allow_bytes_upload", False)
                        and not self._sandbox_bytes_unavailable
                    ):
                        return await self.upload_video_bytes_for_sandbox(file_or_url)
                    return False
                # DIRECT_POST habilitado explícitamente en sandbox
                logger.info(
                    "Estrategia DIRECT_POST habilitada en SANDBOX. Intentando publicación directa."
                )
                if not self._has_scope("video.publish"):
                    logger.warning(
                        "El token no tiene scope 'video.publish'. Omitiendo DIRECT_POST."
                    )
                    return False
                post_details = {
                    "title": self.config.description_template.format(
                        streamer_name=streamer,
                        game_name="Gaming",
                        clip_title="¡Momentazo!",
                    ),
                    "privacy_level": "SELF_ONLY",
                }
                publish_id = await self.upload_video(
                    file_or_url, direct_post=True, **post_details
                )
                if publish_id:
                    return True
                logger.warning(
                    "DIRECT_POST falló. Intentando MULTIPART o BYTES según corresponda."
                )
                backoff_sec = getattr(self.config, "sandbox_spam_backoff_seconds", 900)
                can_try_multipart = (
                    not self._last_sandbox_spam_risk_ts
                    or (time.time() - self._last_sandbox_spam_risk_ts) >= backoff_sec
                )
                if can_try_multipart:
                    publish_id = await self.upload_video(file_or_url, direct_post=False)
                    if publish_id:
                        return True
                if getattr(self.config, "sandbox_allow_bytes_upload", False):
                    return await self.upload_video_bytes_for_sandbox(file_or_url)
                return False

            elif strategy == "PULL_FROM_URL":
                logger.warning(
                    "PULL_FROM_URL no está soportado en SANDBOX en esta implementación. Usando MULTIPART (inbox) como fallback."
                )
                publish_id = await self.upload_video(file_or_url, direct_post=False)
                if publish_id:
                    return True
                logger.warning(
                    "Fallo la inicialización MULTIPART en SANDBOX tras fallback desde PULL_FROM_URL. Probando subida directa por bytes."
                )
                if (
                    getattr(self.config, "sandbox_allow_bytes_upload", False)
                    and not self._sandbox_bytes_unavailable
                ):
                    bytes_ok = await self.upload_video_bytes_for_sandbox(file_or_url)
                    if bytes_ok:
                        return True
                logger.warning(
                    "Subida directa por bytes también falló o está deshabilitada. Intentando publicación directa (requiere scope 'video.publish')."
                )
                post_details = {
                    "title": self.config.description_template.format(
                        streamer_name=streamer,
                        game_name="Gaming",
                        clip_title="¡Momentazo!",
                    ),
                    "privacy_level": "SELF_ONLY",
                }
                publish_id = await self.upload_video(
                    file_or_url, direct_post=True, **post_details
                )
                return bool(publish_id)

            else:
                # Estrategia por defecto: intentar BYTES si está habilitado; luego MULTIPART; por último DIRECT_POST
                if (
                    getattr(self.config, "sandbox_allow_bytes_upload", False)
                    and not self._sandbox_bytes_unavailable
                ):
                    bytes_ok = await self.upload_video_bytes_for_sandbox(file_or_url)
                    if bytes_ok:
                        return True
                backoff_sec = getattr(self.config, "sandbox_spam_backoff_seconds", 900)
                can_try_multipart = (
                    not self._last_sandbox_spam_risk_ts
                    or (time.time() - self._last_sandbox_spam_risk_ts) >= backoff_sec
                )
                if can_try_multipart:
                    publish_id = await self.upload_video(file_or_url, direct_post=False)
                    if publish_id:
                        return True
                else:
                    remaining = int(
                        backoff_sec - (time.time() - self._last_sandbox_spam_risk_ts)
                    )
                    logger.warning(
                        f"Omitiendo MULTIPART por backoff de spam_risk. Reintenta en ~{remaining}s."
                    )
                if getattr(
                    self.config, "sandbox_allow_direct_post", False
                ) and self._has_scope("video.publish"):
                    logger.warning(
                        "MULTIPART (inbox) falló o está en backoff en SANDBOX. Intentando publicación directa (requiere scope 'video.publish')."
                    )
                    post_details = {
                        "title": self.config.description_template.format(
                            streamer_name=streamer,
                            game_name="Gaming",
                            clip_title="¡Momentazo!",
                        ),
                        "privacy_level": "SELF_ONLY",
                    }
                    publish_id = await self.upload_video(
                        file_or_url, direct_post=True, **post_details
                    )
                    return bool(publish_id)
                else:
                    logger.info(
                        "DIRECT_POST deshabilitado o sin scope en SANDBOX. Finalizando tras intentos de BYTES/MULTIPART."
                    )
                    return False

        else:
            # Producción: usar publicación directa
            if not self._has_scope("video.publish"):
                logger.error(
                    "El token de producción no tiene scope 'video.publish'. No se puede publicar directamente."
                )
                return False
            post_details = {
                "title": self.config.description_template.format(
                    streamer_name=streamer,
                    game_name="Gaming",
                    clip_title="¡Momentazo!",
                ),
                "privacy_level": "SELF_ONLY",
            }
            publish_id = await self.upload_video(
                file_or_url, direct_post=True, **post_details
            )
            return bool(publish_id)

    async def upload_video_bytes_for_sandbox(
        self, video_path: str, allow_multipart_fallback: bool = True
    ) -> bool:
        """
        Sube los bytes de un video al endpoint de sandbox /video/upload/.
        Este método es más simple y evita el flujo de 'post' que causa 'spam_risk'.
        Limita el tamaño a 50MB para evitar errores de conexión. Implementa reintentos
        ante errores 5xx y un fallback de transcodificación a menor bitrate/resolución.
        """
        if not os.path.exists(video_path):
            logger.error(f"Error: El archivo no existe en {video_path}")
            return False

        file_size = os.path.getsize(video_path)
        max_sandbox_mb = 50
        if file_size > max_sandbox_mb * 1024 * 1024:
            logger.warning(
                f"El archivo {video_path} excede el límite de {max_sandbox_mb}MB para el sandbox de TikTok ({file_size / (1024 * 1024):.2f}MB). Intentando transcodificar para reducir tamaño..."
            )
            transcoded = await self._transcode_for_sandbox(Path(video_path))
            if not transcoded:
                logger.error("No fue posible transcodificar el video para el sandbox.")
                return False
            video_path = str(transcoded)
            file_size = os.path.getsize(video_path)
            logger.info(
                f"Tamaño tras transcodificación: {file_size / (1024 * 1024):.2f}MB"
            )

        access_token = await self._get_valid_access_token()
        if not access_token:
            return False

        # Nota: el endpoint de subida directa por bytes usa el dominio open.tiktokapis.com
        # incluso cuando el entorno general es SANDBOX para los endpoints de publish/init.
        upload_url = "https://open.tiktokapis.com/v2/video/upload/"
        headers = {"Authorization": f"Bearer {access_token}"}

        # Reintentos manuales para controlar condiciones de error 5xx/timeout
        last_error_text = None
        used_transcoded_path = None
        for attempt in range(1, 4):
            try:
                logger.info(f"Subiendo video al SANDBOX (intento {attempt}/3)...")
                with open(video_path, "rb") as f:
                    files = {"video": (os.path.basename(video_path), f, "video/mp4")}

                    def do_upload():
                        with httpx.Client(
                            timeout=httpx.Timeout(
                                30.0, connect=60.0, read=300.0, write=300.0
                            )
                        ) as client:
                            return client.post(upload_url, headers=headers, files=files)

                    response = await asyncio.to_thread(do_upload)
                    status = response.status_code
                    if status >= 500:
                        # Fuerza reintento
                        last_error_text = response.text
                        logger.warning(
                            f"Respuesta 5xx del sandbox ({status}). Reintentando..."
                        )
                        await asyncio.sleep(2 * attempt)
                        continue
                    response.raise_for_status()

                response_data = response.json()
                if response_data.get("error", {}).get("code") != "ok":
                    logger.error(
                        f"Error en la respuesta de subida de video a sandbox: {response_data['error']}"
                    )
                    # Si error es por tamaño o validación, intentar fallback de transcodificación una vez
                    if attempt == 1:
                        transcoded = await self._transcode_for_sandbox(Path(video_path))
                        if transcoded and os.path.getsize(transcoded) < os.path.getsize(
                            video_path
                        ):
                            video_path = str(transcoded)
                            used_transcoded_path = video_path
                            logger.info(
                                "Reintentando subida con versión transcodificada más ligera..."
                            )
                            continue
                    return False

                video_id = (
                    response_data.get("data", {}).get("video", {}).get("video_id")
                )
                if video_id:
                    logger.success(
                        f"Video subido exitosamente al sandbox de TikTok. Video ID: {video_id}"
                    )
                    # Limpieza opcional del archivo transcodificado temporal
                    try:
                        if (
                            used_transcoded_path
                            and used_transcoded_path.endswith("_compressed.mp4")
                            and os.path.exists(used_transcoded_path)
                        ):
                            os.remove(used_transcoded_path)
                            logger.debug(
                                f"Eliminado archivo temporal: {used_transcoded_path}"
                            )
                    except Exception as _:
                        pass
                    return True
                else:
                    logger.error("No se recibió un video_id en la respuesta de subida.")
                    return False

            except httpx.HTTPStatusError as e:
                code = e.response.status_code if e.response is not None else "N/A"
                last_error_text = e.response.text if e.response is not None else str(e)
                # Si es 404/403, es indicativo de endpoint no habilitado o sin permisos; no reintentar.
                if code in (403, 404):
                    logger.error(
                        "Subida por bytes al sandbox respondió {}. "
                        "Este endpoint puede no estar habilitado para tu app/entorno o faltar permisos. "
                        "No se reintentará más este método.",
                        code,
                    )
                    # Marcar como no disponible y persistir para evitar futuros intentos en esta sesión
                    self._sandbox_bytes_unavailable = True
                    self._save_sandbox_state()
                    break
                logger.warning(
                    f"Error HTTP al subir video a sandbox (intento {attempt}/3): {code}. Reintentando..."
                )
                await asyncio.sleep(2 * attempt)
            except Exception as e:
                last_error_text = str(e)
                logger.warning(
                    f"Error inesperado al subir video a sandbox (intento {attempt}/3): {e}. Reintentando..."
                )
                await asyncio.sleep(2 * attempt)

        logger.error(
            "Fallaron todos los intentos de subida directa al sandbox. {}",
            "Intentando fallback de subida fragmentada..."
            if allow_multipart_fallback
            else "No se realizará fallback a MULTIPART (deshabilitado).",
        )
        # Fallback: intentar flujo de subida fragmentada (inbox) si está permitido y no hay backoff activo
        if allow_multipart_fallback:
            try:
                backoff_sec = getattr(self.config, "sandbox_spam_backoff_seconds", 900)
                if (
                    not self._last_sandbox_spam_risk_ts
                    or (time.time() - self._last_sandbox_spam_risk_ts) >= backoff_sec
                ):
                    publish_id = await self.upload_video(video_path, direct_post=False)
                    if publish_id:
                        logger.success(
                            f"Fallback exitoso: subida fragmentada inicializada. Publish ID: {publish_id}"
                        )
                        return True
                else:
                    remaining = int(
                        backoff_sec - (time.time() - self._last_sandbox_spam_risk_ts)
                    )
                    logger.warning(
                        f"Omitiendo fallback a MULTIPART por backoff de spam_risk. Reintenta en ~{remaining}s."
                    )
            except Exception as e:
                logger.error(f"Fallback de subida fragmentada también falló: {e}")

        if last_error_text:
            logger.error(f"Último error de sandbox: {last_error_text}")
        return False

    async def _transcode_for_sandbox(self, input_path: Path) -> Path | None:
        """Transcodifica el video a 720x1280 (o menor) con CRF 28 y audio 128k para reducir tamaño.
        Retorna el nuevo Path o None si falla.
        """
        try:
            parent = input_path.parent
            output_path = parent / f"{input_path.stem}_compressed.mp4"
            # Si ya existe, intente reutilizarlo
            if output_path.exists():
                return output_path

            # Comando ffmpeg: mantener AR 9:16 objetivo como salida 1080x1920? Mejor bajar a 720x1280
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                str(input_path),
                "-vf",
                "scale='min(720,iw)':-2:flags=lanczos",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "28",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                str(output_path),
            ]
            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            if process.returncode != 0:
                logger.error(
                    f"Fallo al transcodificar para sandbox: {stderr.decode(errors='ignore')}"
                )
                return None
            # Validar tamaño
            size_mb = os.path.getsize(output_path) / (1024 * 1024)
            logger.info(
                f"Transcodificación completada: {output_path.name} ({size_mb:.2f}MB)"
            )
            return output_path
        except Exception as e:
            logger.error(f"Error en transcodificación para sandbox: {e}")
            return None

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

        # Calcular tamaño de chunk y número de chunks asegurando que
        # - chunk_size nunca exceda el tamaño del archivo
        # - si el archivo es pequeño, usar un único chunk
        preferred_chunk_size = 20 * 1024 * 1024  # 20 MB recomendado
        if file_size <= self.MIN_CHUNK_SIZE:
            # Archivos pequeños: un solo chunk exactamente del tamaño del archivo
            chunk_size = file_size
            total_chunks = 1
        else:
            # Archivos medianos/grandes: usar tamaño recomendado acotado por límites
            # y nunca mayor que el tamaño del archivo
            bounded_preferred = max(
                self.MIN_CHUNK_SIZE, min(preferred_chunk_size, self.MAX_CHUNK_SIZE)
            )
            chunk_size = min(bounded_preferred, file_size)
            total_chunks = max(1, math.ceil(file_size / chunk_size))

        if direct_post:
            logger.info("Intentando publicación directa...")
            init_url = f"{self.base_url}/post/publish/video/init/"
            # CORRECCIÓN: El payload para publicación directa debe contener 'post_info'.
            # Enriquecemos post_info con campos comunes para robustez.
            enriched_post_info = {
                **post_info,
                "caption": post_info.get("title", ""),
                "disable_duet": True,
                "disable_stitch": True,
                "allow_comments": False,
                "content_language": "es",
            }
            payload = {
                "post_info": enriched_post_info,
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": file_size,
                },
            }
        else:
            logger.info("Intentando subir como borrador a la bandeja de entrada...")
            init_url = f"{self.base_url}/post/publish/inbox/video/init/"
            # CORRECCIÓN: El payload para borrador (inbox) debe contener los detalles de los chunks.
            payload = {
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": file_size,
                    "chunk_size": chunk_size,
                    "total_chunk_count": total_chunks,
                }
            }
        # Log de depuración para validar parámetros de chunk antes del INIT
        try:
            logger.debug(
                f"INIT TikTok -> direct_post={direct_post}, file_size={file_size} bytes, chunk_size={chunk_size} bytes, total_chunks={total_chunks}"
            )
        except Exception:
            pass
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
            # Intento best-effort de "finalizar" o "confirmar" la subida en el flujo de inbox
            # para evitar acumular shares pendientes que disparan spam_risk.
            if not direct_post:
                try:
                    finalized = await self._finalize_inbox_upload(publish_id)
                    if finalized:
                        logger.info(
                            f"Finalización/confirmación de inbox completada para publish_id={publish_id}."
                        )
                    else:
                        logger.warning(
                            "No se pudo confirmar/finalizar el inbox publish tras la subida. Podrían quedar elementos pendientes."
                        )
                except Exception as e:
                    logger.warning(
                        f"Fallo inesperado al intentar finalizar el inbox publish: {e}"
                    )
            return publish_id
        else:
            logger.error("La subida del video falló.")
            return None

    async def _finalize_inbox_upload(self, publish_id: str) -> bool:
        """Intento best-effort para finalizar o confirmar una subida a inbox y evitar pendientes.
        Dado que la documentación del sandbox puede variar, probamos múltiples endpoints comunes.

        Retorna True si alguno responde OK y el código es "ok"; False en caso contrario.
        """
        access_token = await self._get_valid_access_token()
        if not access_token:
            return False

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        }
        payload = {"publish_id": publish_id}

        candidate_paths = [
            # Completar/confirmar subida multipart al inbox (nombres probables)
            f"{self.base_url}/post/publish/inbox/video/complete/",
            f"{self.base_url}/post/publish/inbox/video/commit/",
            f"{self.base_url}/post/publish/inbox/confirm/",
        ]

        for url in candidate_paths:
            try:
                resp = await self.client.post(
                    url, headers=headers, data=json.dumps(payload)
                )
                if resp.status_code == 404:
                    # Endpoint no existente; probar siguiente
                    continue
                resp.raise_for_status()
                body = resp.json()
                if body.get("error", {}).get("code") == "ok":
                    return True
            except httpx.HTTPStatusError as e:
                # Si el endpoint existe pero falla, loguear y seguir con otro
                logger.debug(
                    f"Intento de finalizar inbox en {url} rechazado: {e.response.status_code} - {e.response.text}"
                )
            except Exception as e:
                logger.debug(f"Error al intentar finalizar inbox en {url}: {e}")

        return False
