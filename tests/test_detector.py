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
    keywords: dict = field(default_factory=lambda: {"clutch": 3.0}) # Añadimos keyword para el test

@dataclass
class MockDetectionConfig:
    clip_duration_seconds: int = 10
    hype_score_threshold: float = 1.5
    rms_peak_threshold: float = 0.7
    scoring: MockScoringConfig = field(default_factory=MockScoringConfig)

@dataclass
class MockTranscriptionConfig:
    whisper_model: str = "tiny"
    device: str = "cpu"
    compute_type: str = "int8"

@dataclass
class MockAppConfig:
    detection: MockDetectionConfig
    transcription: MockTranscriptionConfig

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
        detection=mock_detection_config,
        transcription=mock_transcription_config
    )
    
    # Simulamos la clase Transcriber para no cargar el modelo real
    with patch("streamliner.detector.Transcriber", new_callable=AsyncMock) as mock_transcriber_class:
        
        # Configuramos el mock para que devuelva una transcripción con la palabra "clutch"
        mock_transcriber_instance = mock_transcriber_class.return_value
        mock_transcriber_instance.transcribe.return_value = {"segments": [{"text": "clutch", "start": 30}]}

        # Ahora creamos el detector. Usará el Transcriber falso.
        detector = HighlightDetector(mock_app_config)

    video_duration_sec = 60
    mock_rms_scores = np.zeros(60)
    mock_rms_scores[30] = 1.0

    # 2. Acción (Act)
    # Ajustamos el umbral para asegurar que el pico sea detectado
    mock_detection_config.hype_score_threshold = 0.8
    
    # Hacemos patch a las funciones que interactúan con archivos/CPU para aislarlas
    with patch.object(detector, '_calculate_rms', new_callable=AsyncMock, return_value=mock_rms_scores):
        # ESTE ES EL PATCH QUE PIDE EL BOT DE GITHUB:
        with patch.object(detector, '_extract_audio_segment', new_callable=AsyncMock, return_value="mock_segment.wav") as mock_extract:
            highlights = await detector.find_highlights("fake_audio.wav", video_duration_sec)

    # 3. Aserción (Assert)
    # Verificamos que la función de extracción fue llamada
    mock_extract.assert_called_once()
    
    # Verificamos que se encontró nuestro highlight
    assert len(highlights) == 1
    highlight = highlights[0]
    
    assert highlight['start'] == 25.0
    assert highlight['end'] == 35.0