# src/streamliner/config.py

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union, Literal, Optional

import yaml
from dotenv import load_dotenv
from loguru import logger

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
    logo_path: Optional[Path] = None
    subtitle_style: str = ""
    fg_zoom_factor: float = 1.0
    fg_offset_x: Union[Literal["center"], int] = "center"
    fg_offset_y: Union[Literal["center"], int] = "center"
    # Parámetros para control de subtítulos en pantalla
    subtitle_max_lines_per_cue: int = 2  # Máximo de líneas visibles por evento
    subtitle_max_chars_per_line: int = 32  # Wrapping por palabras
    subtitle_safe_margin_v: int = 120  # Margen inferior (en px de video 1080x1920)
    subtitle_font_size: int = 48  # Tamaño de fuente base para 1080x1920
    # Modo y colores para karaoke/keywords
    subtitle_mode: str = "plain"  # 'plain' o 'karaoke'
    subtitle_color_base: str = "&H00FFFFFF"  # Blanco sin alpha (AA=00)
    subtitle_color_highlight: str = "&H0000FFFF"  # Amarillo para resaltado


@dataclass
class PublishingConfig:
    description_template: str
    upload_strategy: str
    upload_cooldown_seconds: int = 0
    # Backoff en segundos para reintentar MULTIPART en sandbox tras 'spam_risk_too_many_pending_share'
    sandbox_spam_backoff_seconds: int = 900
    # Permitir el endpoint experimental de subida por BYTES en sandbox (v2/video/upload/)
    # Algunas apps no lo tienen habilitado y devuelve 404; por defecto lo desactivamos.
    sandbox_allow_bytes_upload: bool = False
    # Permitir intentos de publicación directa en SANDBOX. Por defecto desactivado,
    # ya que muchos entornos sandbox solo aceptan borradores (inbox) y no DIRECT_POST.
    sandbox_allow_direct_post: bool = False


@dataclass
class RealTimeProcessingConfig:
    chunk_duration_seconds: int = 30
    highlight_buffer_size: int = 5  # Ya está aquí, pero es el maxlen del deque
    min_chunks_for_detection: int = (
        2  # <-- AÑADIDO: Número mínimo de chunks para iniciar la detección
    )


# --- NUEVA CLASE PARA GESTIONAR TODAS LAS RUTAS ---
@dataclass
class PathsConfig:
    data_dir: Path = Path("data")
    chunks_dir: Path = Path("data/chunks")
    clips_dir: Path = Path("data/clips_generados")
    local_storage_base_dir: Path = Path("data/local_storage")
    transcriber_models_dir: Path = Path("data/transcriber_models")


@dataclass
class AppConfig:
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
    paths: PathsConfig  # <-- Añadimos la nueva configuración de rutas
    log_level: str = "INFO"
    log_json: bool = False
    dry_run: bool = False


def load_config() -> AppConfig:
    logger.info("Cargando configuración...")
    config_path = Path("config.yaml")
    if not config_path.exists():
        raise FileNotFoundError("El archivo 'config.yaml' no se encuentra.")

    with open(config_path, "r", encoding="utf-8") as f:
        yaml_config = yaml.safe_load(f)

    # --- Procesamiento de Rutas ---
    paths_data = yaml_config.get("paths", {})
    if "data_dir" in paths_data:
        paths_data["data_dir"] = Path(paths_data["data_dir"])
    if "chunks_dir" in paths_data:
        paths_data["chunks_dir"] = Path(paths_data["chunks_dir"])
    if "clips_dir" in paths_data:
        paths_data["clips_dir"] = Path(paths_data["clips_dir"])
    if "local_storage_base_dir" in paths_data:
        paths_data["local_storage_base_dir"] = Path(
            paths_data["local_storage_base_dir"]
        )
    if "transcriber_models_dir" in paths_data:
        paths_data["transcriber_models_dir"] = Path(
            paths_data["transcriber_models_dir"]
        )

    # --- Procesamiento de Detección ---
    detection_yaml = yaml_config.get("detection", {})
    scoring_yaml = detection_yaml.pop("scoring", {})
    general_keywords_data = detection_yaml.pop("keywords", {})
    streamer_keywords_data = detection_yaml.pop("streamer_keywords", {})

    storage_type = os.getenv("STORAGE_TYPE", "local")

    # Cargar las credenciales de TikTok desde las variables de entorno
    tiktok_creds = TikTokCredentials(
        client_key=os.getenv("TIKTOK_CLIENT_KEY"),
        client_secret=os.getenv("TIKTOK_CLIENT_SECRET"),
        access_token=os.getenv("TIKTOK_ACCESS_TOKEN"),
        refresh_token=os.getenv("TIKTOK_REFRESH_TOKEN"),
        open_id=os.getenv("TIKTOK_OPEN_ID"),
        environment=os.getenv("TIKTOK_ENVIRONMENT", "sandbox"),  # Default a "sandbox"
    )

    # Ahora puedes añadir validación si quieres que sean obligatorios
    if not tiktok_creds.client_key:
        logger.error("TIKTOK_CLIENT_KEY no está configurado en el archivo .env")
        raise ValueError("Missing TIKTOK_CLIENT_KEY")
    if not tiktok_creds.client_secret:
        logger.error("TIKTOK_CLIENT_SECRET no está configurado en el archivo .env")
        raise ValueError("Missing TIKTOK_CLIENT_SECRET")
    if not tiktok_creds.access_token:
        logger.error("TIKTOK_ACCESS_TOKEN no está configurado en el archivo .env")
        raise ValueError("Missing TIKTOK_ACCESS_TOKEN")
    if not tiktok_creds.open_id:
        logger.error("TIKTOK_OPEN_ID no está configurado en el archivo .env")
        raise ValueError("Missing TIKTOK_OPEN_ID")
    # refresh_token podría no ser estrictamente necesario para la primera subida, pero es bueno tenerlo
    if not tiktok_creds.refresh_token:
        logger.warning(
            "TIKTOK_REFRESH_TOKEN no está configurado en el archivo .env. La renovación de tokens podría fallar."
        )

    config = AppConfig(
        streamers=yaml_config.get("streamers", []),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        log_json=os.getenv("LOG_JSON", "false").lower() == "true",
        paths=PathsConfig(**paths_data),
        storage=StorageConfig(
            storage_type=storage_type,
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
            aws_s3_bucket_name=os.getenv("AWS_S3_BUCKET_NAME"),
            aws_s3_region=os.getenv("AWS_S3_REGION"),
            aws_s3_endpoint_url=os.getenv("AWS_S3_ENDPOINT_URL"),
        ),
        credentials={
            "tiktok": tiktok_creds  # <-- ¡Ahora pasamos el objeto tiktok_creds ya cargado!
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
        rendering=RenderingConfig(**yaml_config.get("rendering", {})),
        publishing=PublishingConfig(**yaml_config.get("publishing", {})),
        real_time_processing=RealTimeProcessingConfig(
            **yaml_config.get("real_time_processing", {})
        ),
    )

    # Crear directorios base si no existen
    config.paths.chunks_dir.mkdir(parents=True, exist_ok=True)
    config.paths.clips_dir.mkdir(parents=True, exist_ok=True)
    config.paths.local_storage_base_dir.mkdir(parents=True, exist_ok=True)
    config.paths.transcriber_models_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Configuración cargada exitosamente.")
    return config
    logger.info("Configuración cargada exitosamente.")
    return config
