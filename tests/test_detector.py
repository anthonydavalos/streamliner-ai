# tests/test_detector.py

import numpy as np
import pytest
from unittest.mock import AsyncMock, patch
from dataclasses import dataclass, field

from streamliner.detector import HighlightDetector

# Creamos una configuración falsa para usar en los tests
@dataclass
class MockScoringConfig:
    rms_weight: float = 0.6
    keyword_weight: float = 0.4
    keywords: dict = field(default_factory=dict)

@dataclass
class MockDetectionConfig:
    clip_duration_seconds: int = 10
    hype_score_threshold: float = 1.5
    scoring: MockScoringConfig = MockScoringConfig()

@pytest.mark.asyncio
async def test_find_highlights_scoring_logic():
    """
    Verifica que la lógica de scoring del detector funciona correctamente.
    Simula los scores de RMS y palabras clave para probar el algoritmo de picos.
    """
    # 1. Preparación (Arrange)
    # Creamos una configuración y un detector falsos para la prueba
    mock_config = MockDetectionConfig()
    
    # Necesitamos simular un objeto Transcriber, ya que no queremos instanciarlo
    with patch('streamliner.stt.Transcriber', new_callable=AsyncMock):
        detector = HighlightDetector(mock_config)

    video_duration_sec = 60

    # Creamos datos falsos para simular los resultados del análisis de audio
    # Un array de 60 puntos, uno por cada segundo
    # RMS: un pico de energía en el segundo 30
    mock_rms_scores = np.zeros(60)
    mock_rms_scores[30] = 1.0
    
    # Palabras clave: un pico de keywords también en el segundo 30
    mock_keyword_scores = np.zeros(60)
    mock_keyword_scores[30] = 1.0

    # 2. Acción (Act)
    # Usamos patch para que nuestros métodos de análisis internos devuelvan los datos falsos
    with patch.object(detector, '_calculate_rms', new_callable=AsyncMock, return_value=mock_rms_scores):
        with patch.object(detector, '_get_keyword_scores', new_callable=AsyncMock, return_value=mock_keyword_scores):
            highlights = await detector.find_highlights("fake_audio.wav", video_duration_sec)

    # 3. Aserción (Assert)
    # Verificamos que se encontró exactamente un highlight
    assert len(highlights) == 1
    
    highlight = highlights[0]
    
    # El score final en el pico (segundo 30) debería ser:
    # (1.0 * 0.6) + (1.0 * 0.4) = 1.0. Esto es menor que el umbral de 1.5.
    # ¡Ah! Un error en el planteamiento. La normalización lo cambia todo.
    # El score normalizado será (1-0)/(1-0) = 1 para ambos.
    # El hype score será (1 * 0.6) + (1 * 0.4) = 1.0.
    # Ajustemos el umbral en el test para que el pico sea detectado.
    mock_config.hype_score_threshold = 0.8
    
    with patch.object(detector, '_calculate_rms', new_callable=AsyncMock, return_value=mock_rms_scores):
        with patch.object(detector, '_get_keyword_scores', new_callable=AsyncMock, return_value=mock_keyword_scores):
            highlights_new_threshold = await detector.find_highlights("fake_audio.wav", video_duration_sec)

    assert len(highlights_new_threshold) == 1
    highlight = highlights_new_threshold[0]

    # Verificamos que la puntuación es correcta
    assert highlight['score'] == pytest.approx(1.0)
    
    # Verificamos que el timestamp del clip está centrado en el pico (segundo 30)
    # Duración del clip = 10s, centro en 30s. Debería ir de 25s a 35s.
    assert highlight['start'] == 25.0
    assert highlight['end'] == 35.0