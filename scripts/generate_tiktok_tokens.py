# scripts/generate_tiktok_tokens.py
import asyncio
import os
import httpx
from dotenv import load_dotenv, set_key
from loguru import logger
import urllib.parse
from pathlib import Path

# Carga las variables de entorno para usar las CLIENT_KEY y CLIENT_SECRET
load_dotenv()

# --- Configuración ---
TIKTOK_BASE_URL = "https://open-api.tiktok.com"
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"

# Obtén las claves del .env. Si no están, el usuario las tendrá que poner manualmente.
CLIENT_KEY = os.getenv("TIKTOK_CLIENT_KEY")
CLIENT_SECRET = os.getenv("TIKTOK_CLIENT_SECRET")
REDIRECT_URI = (
    "https://www.example.com/oauth"  # Mantén tu URI de redirección registrada
)

# Define el path al archivo .env para escribir los tokens
ENV_PATH = Path(".env")


async def generate_tokens():
    if not CLIENT_KEY or not CLIENT_SECRET:
        logger.error(
            "Error: TIKTOK_CLIENT_KEY y/o TIKTOK_CLIENT_SECRET no están definidos en el archivo .env."
        )
        logger.error(
            "Por favor, asegúrate de tenerlos configurados desde el TikTok Developer Center."
        )
        return

    logger.info("--- Generador de Tokens de TikTok ---")
    logger.info("Paso 1: Obtener el Código de Autorización.")

    # Construir la URL de autorización
    params = {
        "client_key": CLIENT_KEY,
        "scope": "user.info.basic,video.upload,video.list,user.info.profile,user.info.stats",  # Asegúrate de tener todos los scopes necesarios
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
    }
    encoded_params = urllib.parse.urlencode(params)
    full_auth_url = f"{AUTH_URL}?{encoded_params}"

    logger.info(
        "Por favor, abre la siguiente URL en tu navegador y autoriza la aplicación:"
    )
    logger.info(f"\n{full_auth_url}\n")
    logger.info(
        "Después de autorizar, serás redirigido a una página. Copia el 'code' y el 'open_id' de la URL de redirección."
    )

    auth_code = input("Introduce el 'code' (código de autorización) aquí: ").strip()
    open_id = input("Introduce el 'open_id' aquí: ").strip()

    if not auth_code or not open_id:
        logger.error("Código de autorización u Open ID no proporcionado. Cancelando.")
        return

    logger.info("Paso 2: Intercambiar Código de Autorización por Tokens.")
    token_url = f"{TIKTOK_BASE_URL}/oauth/access_token/"
    token_payload = {
        "client_key": CLIENT_KEY,
        "client_secret": CLIENT_SECRET,
        "code": auth_code,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
    }

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(token_url, json=token_payload)
            response.raise_for_status()
            token_data = response.json()

            if token_data.get("error"):
                logger.error(f"Error al obtener tokens: {token_data['error']}")
                if "error_description" in token_data:
                    logger.error(
                        f"Descripción del error: {token_data['error_description']}"
                    )
                return

            access_token = token_data.get("access_token")
            refresh_token = token_data.get("refresh_token")
            expires_in = token_data.get("expires_in")
            refresh_expires_in = token_data.get("refresh_expires_in")

            if not all([access_token, refresh_token, expires_in, refresh_expires_in]):
                logger.error(f"Faltan datos en la respuesta de tokens: {token_data}")
                return

            logger.success("Tokens obtenidos con éxito!")
            logger.info(
                f"Access Token (caduca en {expires_in}s): {access_token[:8]}..."
            )
            logger.info(
                f"Refresh Token (caduca en {refresh_expires_in}s): {refresh_token[:8]}..."
            )
            logger.info(f"Open ID: {open_id}")

            # Paso 3: Guardar los tokens en el archivo .env
            logger.info(f"Guardando tokens en {ENV_PATH}...")
            set_key(ENV_PATH, "TIKTOK_ACCESS_TOKEN", access_token)
            set_key(ENV_PATH, "TIKTOK_REFRESH_TOKEN", refresh_token)
            set_key(ENV_PATH, "TIKTOK_OPEN_ID", open_id)

            logger.success("Tokens guardados exitosamente en .env!")

        except httpx.HTTPStatusError as e:
            logger.error(
                f"Error HTTP al obtener tokens: {e.response.status_code} - {e.response.text}"
            )
        except Exception as e:
            logger.error(f"Error inesperado durante la obtención de tokens: {e}")


if __name__ == "__main__":
    asyncio.run(generate_tokens())
