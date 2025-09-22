# src/scripts/generate_tiktok_tokens.py.example
# Este es un script de ejemplo para generar y refrescar tokens de acceso a TikTok.
# POR FAVOR, NO MODIFIQUES ESTE ARCHIVO DIRECTAMENTE EN TU REPOSITORIO.
# En su lugar, copia este archivo a 'src/scripts/generate_tiktok_tokens.py'
# y edita esa copia con tus credenciales.

import requests
import webbrowser
from urllib.parse import urlencode
import time
import os
from pathlib import Path
from dotenv import load_dotenv, set_key
from loguru import logger

# Carga las variables de entorno desde el archivo .env.
# Asegúrate de que CLIENT_KEY y CLIENT_SECRET estén definidos allí.
load_dotenv()

# --- CONFIGURACIÓN ---
# Obtén las claves del .env.
# Necesitas configurar estas variables en tu archivo .env:
# TIKTOK_CLIENT_KEY="tu_client_key_de_tiktok_developer"
# TIKTOK_CLIENT_SECRET="tu_client_secret_de_tiktok_developer"
CLIENT_KEY = os.getenv("TIKTOK_CLIENT_KEY")
CLIENT_SECRET = os.getenv("TIKTOK_CLIENT_SECRET")

# ==============================================================================
# ¡IMPORTANTE! MODIFICA ESTA LÍNEA CON TU PROPIA REDIRECT_URI
# ==============================================================================
# ESTA URL DEBE COINCIDIR EXACTAMENTE CON LA QUE HAS CONFIGURADO
# EN TU APLICACIÓN EN EL TIKTOK DEVELOPER CENTER.
# Ejemplo: "https://tu-usuario.github.io/auth-callback/"
REDIRECT_URI = "https://anthonydavalos.github.io/auth-callback/"
# ==============================================================================

# Scopes de permisos que solicitas.
# En SANDBOX muchas apps no permiten 'video.publish'. Por defecto pedimos solo 'video.upload,user.info.basic'.
# Puedes sobreescribir con la variable de entorno TIKTOK_SCOPES (coma-separado) si quieres incluir 'video.publish'.
DEFAULT_SCOPES = "video.upload,user.info.basic"
SCOPES = os.getenv("TIKTOK_SCOPES", DEFAULT_SCOPES)

# Endpoints de la API de TikTok (estos son los correctos para la API v2)
AUTH_BASE_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"

# Path al archivo .env para escribir los tokens
ENV_PATH = Path(".env")


def get_auth_url():
    """Genera la URL de autorización a la que el usuario debe navegar."""
    params = {
        "client_key": CLIENT_KEY,
        "scope": SCOPES,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "state": f"st_{int(time.time())}",  # Un estado único para cada solicitud (recomendado por seguridad)
    }
    return f"{AUTH_BASE_URL}?{urlencode(params)}"


def get_access_and_refresh_token(auth_code: str):
    """
    Intercambia el código de autorización por tokens de acceso y refresco,
    y los guarda en el archivo .env.
    """
    payload = {
        "client_key": CLIENT_KEY,
        "client_secret": CLIENT_SECRET,
        "code": auth_code,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,  # DEBE COINCIDIR EXACTAMENTE
    }

    logger.info("Enviando solicitud para obtener tokens de acceso y refresco...")
    try:
        response = requests.post(TOKEN_URL, data=payload)
        response.raise_for_status()  # Lanza un error para códigos de estado 4xx/5xx

        token_data = response.json()

        if not token_data.get("access_token"):
            logger.error(
                f"La respuesta no contiene 'access_token'. Respuesta completa: {token_data}"
            )
            return None

        logger.success("¡Tokens obtenidos con éxito!")
        logger.info("=" * 60)
        logger.info(
            f"Guardando las siguientes variables en tu archivo .env ({ENV_PATH}):"
        )

        # Guardar en .env usando set_key de python-dotenv (siempre con comillas dobles)
        set_key(
            ENV_PATH,
            "TIKTOK_ACCESS_TOKEN",
            token_data.get("access_token"),
            quote_mode="always",
        )
        set_key(
            ENV_PATH,
            "TIKTOK_REFRESH_TOKEN",
            token_data.get("refresh_token"),
            quote_mode="always",
        )
        set_key(
            ENV_PATH, "TIKTOK_OPEN_ID", token_data.get("open_id"), quote_mode="always"
        )
        # Asumimos que estamos en sandbox durante la obtención inicial de tokens
        set_key(ENV_PATH, "TIKTOK_ENVIRONMENT", "sandbox", quote_mode="always")
        # Persistir los scopes solicitados para referencia futura
        set_key(ENV_PATH, "TIKTOK_SCOPES", SCOPES, quote_mode="always")

        logger.success("Variables guardadas exitosamente en .env!")
        logger.info("=" * 60)

        return token_data
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Error al obtener el token: {e}")
        if e.response:
            logger.error(f"Detalles de la respuesta de error: {e.response.text}")
        return None


if __name__ == "__main__":
    # Validación y advertencias de scopes
    requested_scopes = {
        s.strip() for s in SCOPES.replace(" ", "").split(",") if s.strip()
    }
    logger.info(f"Scopes solicitados: {sorted(requested_scopes)}")
    if "video.publish" not in requested_scopes:
        logger.info(
            "'video.publish' no solicitado (modo sandbox típico). El publicador omitirá DIRECT_POST automáticamente."
        )

    if REDIRECT_URI == "TU_REDIRECT_URI_AQUI":
        logger.error("ERROR: Debes configurar tu REDIRECT_URI en este script.")
        logger.error(
            "Copia este archivo a 'src/scripts/generate_tiktok_tokens.py' y edita la línea 'REDIRECT_URI'."
        )
        exit(1)  # Salir con error

    if not CLIENT_KEY or not CLIENT_SECRET:
        logger.error(
            "ERROR: TIKTOK_CLIENT_KEY y TIKTOK_CLIENT_SECRET deben estar en tu archivo .env"
        )
        logger.error(
            "Por favor, asegúrate de tenerlos configurados desde el TikTok Developer Center."
        )
    else:
        logger.info(" PASO 1: Autorización de Usuario ".center(80, "="))
        auth_url = get_auth_url()
        logger.info(
            "\n1. Abre la siguiente URL en tu navegador. Si no se abre automáticamente, cópiala y pégala."
        )
        logger.info(f"\n    URL: {auth_url}")
        logger.info(
            "\nScopes solicitados en esta autorización: {}\nSi cambiaste scopes, recuerda que debes re-autorizar para que apliquen.",
            SCOPES,
        )

        try:
            webbrowser.open(auth_url)
        except webbrowser.Error:
            logger.warning(
                "No se pudo abrir el navegador automáticamente. Por favor, copia la URL y pégala manualmente."
            )

        logger.info(
            "\n2. Inicia sesión en TikTok con una CUENTA DE PRUEBA (Target User) del Sandbox (si estás en Sandbox)."
        )
        logger.info("3. Autoriza la aplicación.")
        logger.info(
            "4. Serás redirigido a la URL de callback. Copia el valor del parámetro 'code' de la URL."
        )
        logger.info(
            "   Ejemplo: https://tu_redirect_uri.com/?code=ABCDEFG12345&state=..."
        )
        logger.info("   En este ejemplo, el código es 'ABCDEFG12345'")
        logger.info("\n" + "=" * 80)

        auth_code = input(
            "\n> Pega aquí el código de autorización ('code') y presiona Enter: "
        ).strip()

        if auth_code:
            get_access_and_refresh_token(auth_code)
        else:
            logger.error("\nNo se ingresó código de autorización. Proceso cancelado.")
