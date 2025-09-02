# tests/test_detector.py

import numpy as np
import pytest
from unittest.mock import AsyncMock, patch
from dataclasses import dataclass, field

from streamliner.detector import HighlightDetector

# --- Clases de Configuración Falsas (Mocks) para la Prueba ---


@dataclass
class MockScoringConfig:
    rms_weight: float = 0.6
    keyword_weight: float = 0.4
    keywords: dict = field(default_factory=dict)


@dataclass
class MockDetectionConfig:
    clip_duration_seconds: int = 10
    hype_score_threshold: float = 1.5
    rms_peak_threshold: float = 0.7
    scoring: MockScoringConfig = field(default_factory=MockScoringConfig)


# Añadimos un mock para la configuración de transcripción
@dataclass
class MockTranscriptionConfig:
    whisper_model: str = "tiny"
    device: str = "cpu"
    compute_type: str = "int8"


@dataclass
class MockAppConfig:
    detection: MockDetectionConfig
    # AHORA INCLUIMOS LA CONFIGURACIÓN DE TRANSCRIPCIÓN
    transcription: MockTranscriptionConfig


# --- Fin de las Clases de Prueba ---


@pytest.mark.asyncio
async def test_find_highlights_scoring_logic():
    """
    Verifica que la lógica de scoring del detector funciona correctamente.
    """
    # 1. Preparación (Arrange)
    # Creamos la estructura de configuración completa y correcta.
    mock_detection_config = MockDetectionConfig()
    mock_transcription_config = MockTranscriptionConfig()
    mock_app_config = MockAppConfig(
        detection=mock_detection_config, transcription=mock_transcription_config
    )

    # Simulamos (patch) la clase Transcriber DENTRO del módulo detector,
    # para que su __init__ no intente cargar el modelo real de Whisper.
    with patch(
        "streamliner.detector.Transcriber", new_callable=AsyncMock
    ) as mock_transcriber_instance:
        # Creamos una función falsa para el método 'transcribe'
        async def mock_transcribe(*args, **kwargs):
            return {"segments": [{"text": "clutch", "start": 30}]}

        # Asignamos nuestra función falsa al método del mock
        mock_transcriber_instance.return_value.transcribe = mock_transcribe

        # Ahora creamos el detector. Usará el Transcriber falso.
        detector = HighlightDetector(mock_app_config)

    video_duration_sec = 60
    mock_rms_scores = np.zeros(60)
    mock_rms_scores[30] = 1.0

    # 2. Acción (Act)
    # Ajustamos el umbral en el test para que el pico sea detectado.
    mock_detection_config.hype_score_threshold = 0.8

    with patch.object(
        detector, "_calculate_rms", new_callable=AsyncMock, return_value=mock_rms_scores
    ):
        highlights = await detector.find_highlights(
            "fake_audio.wav", video_duration_sec
        )

    # 3. Aserción (Assert)
    assert len(highlights) == 1
    highlight = highlights[0]

    assert highlight["start"] == 25.0
    assert highlight["end"] == 35.0
