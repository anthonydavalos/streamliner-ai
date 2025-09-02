# src/streamliner/config.py

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv
from loguru import logger

# Cargar las variables de entorno desde el archivo .env
# Esto es lo primero que hacemos para que las variables estén disponibles
load_dotenv()

# --- Definición de Estructuras de Datos (Dataclasses) ---
# Usamos dataclasses para tener una estructura clara y autocompletado en el código.
# Es una forma moderna y limpia de crear clases que principalmente guardan datos.


@dataclass
class TikTokCredentials:
    client_key: str | None = None
    client_secret: str | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    open_id: str | None = None


@dataclass
class StorageConfig:
    storage_type: str
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_s3_bucket_name: str | None = None
    aws_s3_region: str | None = None
    aws_s3_endpoint_url: str | None = None  # Clave para R2


@dataclass
class MonitoringConfig:
    check_interval_seconds: int = 60
    reconnect_delay_seconds: int = 30


@dataclass
class DownloaderConfig:
    output_quality: str = "best"
    local_storage_path: str = "./data"


@dataclass
class ScoringConfig:
    rms_weight: float
    keyword_weight: float
    scene_change_boost: float
    keywords: dict[str, float] = field(default_factory=dict)


@dataclass
class DetectionConfig:
    clip_duration_seconds: int
    hype_score_threshold: float
    rms_peak_threshold: float
    scoring: ScoringConfig
    max_clips_per_vod: int = 3  # Añadimos el nuevo campo con un valor por defecto


@dataclass
class TranscriptionConfig:
    whisper_model: str
    device: str
    compute_type: str


@dataclass
class RenderingConfig:
    logo_path: str | None = None
    subtitle_style: str = ""


@dataclass
class PublishingConfig:
    description_template: str
    upload_strategy: str


# --- Clase Principal de Configuración ---


@dataclass
class AppConfig:
    """
    Clase contenedora principal que agrupa toda la configuración
    de la aplicación en un solo objeto.
    """

    streamers: list[str]
    storage: StorageConfig
    credentials: dict[str, TikTokCredentials]
    monitoring: MonitoringConfig
    downloader: DownloaderConfig
    detection: DetectionConfig
    transcription: TranscriptionConfig
    rendering: RenderingConfig
    publishing: PublishingConfig
    log_level: str = "INFO"
    log_json: bool = False


def load_config() -> AppConfig:
    """
    Carga la configuración desde config.yaml y variables de entorno (.env).
    Valida la configuración y la empaqueta en un objeto AppConfig.
    """
    logger.info("Cargando configuración...")

    # Cargar el archivo de configuración principal (YAML)
    config_path = Path("config.yaml")
    if not config_path.exists():
        raise FileNotFoundError(
            "El archivo 'config.yaml' no se encuentra. Copia 'config.yaml.example' y ajústalo."
        )

    with open(config_path, "r") as f:
        yaml_config = yaml.safe_load(f)

    # --- Preparación de las configuraciones anidadas (Aquí está la corrección) ---
    # Extraemos las secciones del YAML antes de construir el objeto final.
    detection_yaml = yaml_config.get("detection", {})
    scoring_yaml = detection_yaml.pop(
        "scoring", {}
    )  # Extraemos y eliminamos 'scoring' de 'detection_yaml'

    # --- Construcción del objeto de configuración final ---
    storage_type = os.getenv("STORAGE_TYPE", "local")

    config = AppConfig(
        streamers=yaml_config.get("streamers", []),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        log_json=os.getenv("LOG_JSON", "false").lower() == "true",
        storage=StorageConfig(
            storage_type=storage_type,
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
            aws_s3_bucket_name=os.getenv("AWS_S3_BUCKET_NAME"),
            aws_s3_region=os.getenv("AWS_S3_REGION"),
            aws_s3_endpoint_url=os.getenv("AWS_S3_ENDPOINT_URL")
            if storage_type == "r2"
            else None,
        ),
        credentials={
            "tiktok": TikTokCredentials(
                client_key=os.getenv("TIKTOK_CLIENT_KEY"),
                client_secret=os.getenv("TIKTOK_CLIENT_SECRET"),
                access_token=os.getenv("TIKTOK_ACCESS_TOKEN"),
                refresh_token=os.getenv("TIKTOK_REFRESH_TOKEN"),
                open_id=os.getenv("TIKTOK_OPEN_ID"),
            )
        },
        monitoring=MonitoringConfig(**yaml_config.get("monitoring", {})),
        downloader=DownloaderConfig(**yaml_config.get("downloader", {})),
        # Ahora usamos nuestras variables ya preparadas
        detection=DetectionConfig(
            **detection_yaml,  # Pasamos los argumentos de 'detection' (ya sin 'scoring')
            scoring=ScoringConfig(
                **scoring_yaml
            ),  # Pasamos el 'scoring' construido por separado
        ),
        transcription=TranscriptionConfig(**yaml_config.get("transcription", {})),
        rendering=RenderingConfig(**yaml_config.get("rendering", {})),
        publishing=PublishingConfig(**yaml_config.get("publishing", {})),
    )

    logger.info("Configuración cargada exitosamente.")
    return config
