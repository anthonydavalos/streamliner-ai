# tests/test_detector.py

import numpy as np
import pytest
from unittest.mock import AsyncMock, patch
from dataclasses import dataclass, field
from pathlib import Path
import shutil  # Necesario para la limpieza de directorios

from streamliner.detector import HighlightDetector

# --- Clases de Configuración Falsas (Mocks) para la Prueba ---
# Estas clases deben reflejar la estructura de tus clases de configuración reales
# para que los tests puedan crear mocks que el código bajo prueba espera.


@dataclass
class MockScoringConfig:
    rms_weight: float = 0.6
    keyword_weight: float = 0.4
    keywords: dict = field(default_factory=lambda: {"clutch": 3.0})


@dataclass
class MockDetectionConfig:
    clip_duration_seconds: int = 10
    hype_score_threshold: float = 1.5
    rms_peak_threshold: float = 0.7
    scoring: MockScoringConfig = field(
        default_factory=MockScoringConfig
    )  # Contiene 'keywords'
    keywords: dict[str, float] = field(
        default_factory=lambda: {"clutch": 3.0, "epico": 2.0}
    )
    streamer_keywords: dict[str, dict[str, float]] = field(
        default_factory=lambda: {"test": {"fail": -1.0}}
    )
    max_clips_per_vod: int = 3


@dataclass
class MockTranscriptionConfig:
    whisper_model: str = "tiny"
    device: str = "cpu"
    compute_type: str = "int8"
    # Añadimos un directorio de datos específico para el transcriber mock
    # para evitar conflictos y facilitar la limpieza
    data_dir: Path = Path("/mock_test_data/transcriber")


# NOTA: En este test, HighlightDetector NO recibe MockAppConfig directamente
# sino sus sub-componentes (detection y transcription configs).
# MockAppConfig se usa solo para organizar la creación de esas sub-configs para el test.
@dataclass
class MockAppConfig:
    detection: MockDetectionConfig
    transcription: MockTranscriptionConfig

    # paths_config no es pasado al constructor de HighlightDetector directamente
    # pero puede ser necesario para la instanciación de Transcriber si este usa paths.
    @dataclass
    class MockPathsConfig:
        # Usar un directorio raíz para todos los mocks de Path para facilitar la limpieza
        base_dir: Path = field(default_factory=lambda: Path("/mock_test_data"))
        data_dir: Path = field(default_factory=lambda: Path("/mock_test_data/data"))
        chunks_dir: Path = field(
            default_factory=lambda: Path("/mock_test_data/data/chunks")
        )

    paths: MockPathsConfig = field(default_factory=MockPathsConfig)


# Clase TranscriptionResult para el return_value del Transcriber mock
@dataclass
class TranscriptionResult:
    text: str
    segments: list = field(
        default_factory=list
    )  # Lista de diccionarios {text, start, end}


# --- Fin de las Clases de Prueba ---


@pytest.mark.asyncio
async def test_find_highlights_scoring_logic():
    """
    Verifica que la lógica de scoring del detector funciona correctamente.
    """
    # 1. Preparación (Arrange)
    mock_detection_config = MockDetectionConfig()
    mock_transcription_config = MockTranscriptionConfig()
    mock_app_config = MockAppConfig(
        detection=mock_detection_config, transcription=mock_transcription_config
    )

    # Crear los directorios mock necesarios para Path.mkdir() si tu código real los crea
    # o si son pasados a Transcriber o HighlightDetector
    mock_app_config.paths.base_dir.mkdir(parents=True, exist_ok=True)
    mock_app_config.paths.data_dir.mkdir(parents=True, exist_ok=True)
    mock_app_config.paths.chunks_dir.mkdir(parents=True, exist_ok=True)
    mock_transcription_config.data_dir.mkdir(parents=True, exist_ok=True)

    # Mockear la CLASE Transcriber para controlar su instancia interna dentro de HighlightDetector
    with patch("streamliner.detector.Transcriber", spec=True) as MockTranscriberClass:
        # Configurar la instancia mock que será devuelta por MockTranscriberClass()
        mock_transcriber_instance = MockTranscriberClass.return_value
        # Configurar el método transcribe del mock Transcriber
        mock_transcriber_instance.transcribe.return_value = TranscriptionResult(
            text="un texto de ejemplo con la palabra clutch",
            segments=[
                {"text": "un", "start": 0.0, "end": 0.5},
                {"text": "texto", "start": 0.5, "end": 1.0},
                {"text": "de", "start": 1.0, "end": 1.2},
                {"text": "ejemplo", "start": 1.2, "end": 1.8},
                {"text": "con", "start": 1.8, "end": 2.0},
                {"text": "la", "start": 2.0, "end": 2.2},
                {"text": "palabra", "start": 2.2, "end": 2.8},
                {
                    "text": "clutch",
                    "start": 30.0,
                    "end": 30.5,
                },  # Este es el que nos interesa para el scoring
            ],
        )

        # CORRECCIÓN: Pasar la instancia mock del transcriber, no la configuración.
        detector = HighlightDetector(
            mock_app_config.detection,
            mock_transcriber_instance,
        )

        video_duration_sec = 60
        # Simular puntuaciones RMS (volumen)
        mock_rms_scores = np.zeros(60)  # Array de 60 segundos, todos a 0
        mock_rms_scores[30] = 1.0  # Picos de volumen en el segundo 30

        # 2. Acción (Act)
        mock_detection_config.hype_score_threshold = 0.8  # Umbral para la detección

        with patch.object(
            detector,
            "_calculate_rms",
            new_callable=AsyncMock,
            return_value=mock_rms_scores,
        ):
            with patch.object(
                detector,
                "_extract_audio_segment",
                new_callable=AsyncMock,
                return_value=Path("mock_segment_for_test.wav"),
            ) as mock_extract:
                highlights = await detector.find_highlights(
                    "fake_audio.wav",
                    video_duration_sec,
                    streamer_name="test_streamer",
                    temp_dir=mock_app_config.paths.data_dir,
                )

    # 3. Aserción (Assert)
    mock_extract.assert_called_once()  # Verificar que se intentó extraer audio

    assert len(highlights) == 1
    highlight = highlights[0]
    # Esperamos que el clip esté centrado alrededor del segundo 30 (donde está el pico RMS y la palabra clave)
    # clip_duration_seconds es 10, así que debería ser de 25s a 35s
    assert highlight["start_time"] == 25.0
    assert highlight["end_time"] == 35.0

    # Limpiar los directorios mock creados
    # Borra el directorio raíz /mock_test_data para limpiar todo lo creado allí
    shutil.rmtree(mock_app_config.paths.base_dir, ignore_errors=True)
