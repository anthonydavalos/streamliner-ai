# src/streamliner/config.py

import os
from dataclasses import dataclass, field  # Asegúrate de que 'field' esté importado
from pathlib import Path
from typing import Union, Literal  # <--- Importa esto para los tipos de fg_offset_x/y

import yaml
from dotenv import load_dotenv
from loguru import logger

# Cargar las variables de entorno desde el archivo .env
load_dotenv()

# --- Definición de Estructuras de Datos (Dataclasses) ---


@dataclass
class TikTokCredentials:
    client_key: str | None = None
    client_secret: str | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    open_id: str | None = None
    environment: str = "production"


@dataclass
class StorageConfig:
    storage_type: str
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_s3_bucket_name: str | None = None
    aws_s3_region: str | None = None
    aws_s3_endpoint_url: str | None = None


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


@dataclass
class DetectionConfig:
    clip_duration_seconds: int
    hype_score_threshold: float
    rms_peak_threshold: float
    scoring: ScoringConfig
    max_clips_per_vod: int = 3
    keywords: dict[str, float] = field(default_factory=dict)
    streamer_keywords: dict[str, dict[str, float]] = field(default_factory=dict)


@dataclass
class TranscriptionConfig:
    whisper_model: str
    device: str
    compute_type: str


@dataclass
class RenderingConfig:
    logo_path: str | None = None
    subtitle_style: str = ""
    # --- ¡AÑADE ESTOS CAMPOS AQUÍ! ---
    fg_zoom_factor: float = 1.0  # Valor por defecto
    fg_offset_x: Union[Literal["center"], int] = "center"  # Valor por defecto
    fg_offset_y: Union[Literal["center"], int] = "center"  # Valor por defecto
    # -----------------------------------


@dataclass
class PublishingConfig:
    description_template: str
    upload_strategy: str


@dataclass
class RealTimeProcessingConfig:
    chunk_duration_seconds: int = 30
    chunk_storage_path: str = "./data/chunks"


@dataclass
class AppConfig:
    """
    Clase contenedora principal que agrupa toda la configuración.
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
    real_time_processing: RealTimeProcessingConfig
    log_level: str = "INFO"
    log_json: bool = False


def load_config() -> AppConfig:
    """
    Carga la configuración desde config.yaml y variables de entorno (.env).
    Valida la configuración y la empaqueta en un objeto AppConfig.
    """
    logger.info("Cargando configuración...")

    config_path = Path("config.yaml")
    if not config_path.exists():
        raise FileNotFoundError(
            "El archivo 'config.yaml' no se encuentra. Copia 'config.yaml.example' y ajústalo."
        )

    with open(config_path, "r", encoding="utf-8") as f:
        yaml_config = yaml.safe_load(f)

    detection_yaml = yaml_config.get("detection", {})
    scoring_yaml = detection_yaml.pop("scoring", {})

    general_keywords_data = detection_yaml.pop("keywords", {})
    streamer_keywords_data = detection_yaml.pop("streamer_keywords", {})

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
                environment=os.getenv("TIKTOK_ENVIRONMENT", "production"),
            )
        },
        monitoring=MonitoringConfig(**yaml_config.get("monitoring", {})),
        downloader=DownloaderConfig(**yaml_config.get("downloader", {})),
        detection=DetectionConfig(
            **detection_yaml,
            keywords=general_keywords_data,
            streamer_keywords=streamer_keywords_data,
            scoring=ScoringConfig(**scoring_yaml),
        ),
        transcription=TranscriptionConfig(**yaml_config.get("transcription", {})),
        # --- NO NECESITAS MODIFICAR ESTA LÍNEA ---
        # rendering=RenderingConfig(**yaml_config.get("rendering", {})),
        # Como los campos ya están definidos en RenderingConfig, dataclass los acepta directamente.
        rendering=RenderingConfig(**yaml_config.get("rendering", {})),
        # --- FIN DE LA ACLARACIÓN ---
        publishing=PublishingConfig(**yaml_config.get("publishing", {})),
        real_time_processing=RealTimeProcessingConfig(
            **yaml_config.get("real_time_processing", {})
        ),
    )

    logger.info("Configuración cargada exitosamente.")
    return config
