# src/streamliner/detector.py

import asyncio
import os
import numpy as np
import soundfile as sf
from scipy.signal import find_peaks
from loguru import logger
from pathlib import Path

from .stt import Transcriber
from .config import AppConfig

class HighlightDetector:
    """
    Analiza un archivo de audio para detectar momentos de alta "emoción" o "hype".
    Versión optimizada: Primero busca picos de energía (RMS) y luego solo transcribe
    esos segmentos, ahorrando una enorme cantidad de tiempo de procesamiento.
    """
    def __init__(self, config: AppConfig):
        self.config = config.detection
        self.transcriber = Transcriber(config.transcription)

    async def _extract_audio_segment(self, main_audio_path: Path, start: float, end: float) -> Path:
        """Extrae un pequeño segmento del archivo de audio principal a un archivo temporal."""
        segment_path = main_audio_path.parent / f"temp_segment_{start:.0f}_{end:.0f}.wav"
        args = [
            "ffmpeg", "-y", "-i", str(main_audio_path),
            "-ss", str(start),
            "-to", str(end),
            "-c", "copy",
            str(segment_path)
        ]
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await process.communicate()
        if process.returncode != 0:
            logger.error(f"No se pudo extraer el segmento de audio: {segment_path}")
            return None
        return segment_path

    async def _calculate_rms(self, audio_path: str, window_sec=1.0) -> np.ndarray:
        """Calcula la energía RMS (Root Mean Square) en ventanas de tiempo."""
        logger.info("Calculando energía RMS del audio con soundfile/numpy...")
        try:
            audio_data, sample_rate = sf.read(audio_path)
        except Exception as e:
            logger.error(f"No se pudo leer el archivo de audio con soundfile: {e}")
            return np.array([])
        if audio_data.ndim > 1:
            audio_data = audio_data.mean(axis=1)
        
        window_size = int(sample_rate * window_sec)
        num_windows = len(audio_data) // window_size
        if num_windows == 0:
            return np.array([])
        
        rms_values = [np.sqrt(np.mean(audio_data[i*window_size:(i+1)*window_size]**2)) for i in range(num_windows)]
        return np.array(rms_values)

    async def find_highlights(self, audio_path_str: str, video_duration_sec: float) -> list[dict]:
        logger.info("Iniciando detección de highlights (Modo Eficiente)...")
        audio_path = Path(audio_path_str)
        
        # --- PASO 1: Análisis Rápido de Energía en todo el audio ---
        rms_scores = await self._calculate_rms(audio_path_str)
        if rms_scores.size == 0:
            logger.warning("No se pudo calcular el score RMS. Abortando detección.")
            return []

        rms_min, rms_max = np.min(rms_scores), np.max(rms_scores)
        if rms_max - rms_min < 1e-6:
            logger.warning("Audio sin variación de energía significativa.")
            return []
            
        normalized_rms = (rms_scores - rms_min) / (rms_max - rms_min)
        
        # --- PASO 2: Encontrar Picos de Energía (Candidatos a Highlight) ---
        candidate_peaks, _ = find_peaks(normalized_rms, height=self.config.rms_peak_threshold, distance=self.config.clip_duration_seconds)
        
        if not candidate_peaks.any():
            logger.warning("No se encontraron picos de energía que superen el umbral.")
            return []
            
        logger.info(f"Se encontraron {len(candidate_peaks)} candidatos a highlight basados en la energía del audio.")
        
        # --- PASO 3: Análisis Enfocado - Transcribir solo los segmentos candidatos ---
        candidate_highlights = []
        for peak_idx in candidate_peaks:
            center_timestamp = peak_idx # Cada índice es un segundo
            start_time = max(0, center_timestamp - self.config.clip_duration_seconds / 2)
            end_time = min(video_duration_sec, center_timestamp + self.config.clip_duration_seconds / 2)

            segment_audio_path = None
            try:
                # Extraer este pequeño segmento para analizarlo
                segment_audio_path = await self._extract_audio_segment(audio_path, start_time, end_time)
                if not segment_audio_path:
                    continue

                # Transcribir SOLO este pequeño segmento
                transcription = await self.transcriber.transcribe(segment_audio_path)
                logger.debug(f"Texto del segmento transcrito: '{transcription['text']}'")
                
                # Calcular score de palabras clave para este segmento
                keyword_score = 0
                for segment in transcription["segments"]:
                    text = segment["text"].lower()
                    for keyword, weight in self.config.scoring.keywords.items():
                        if keyword in text:
                            keyword_score += weight
                
                # Combinar scores
                final_hype_score = (
                    normalized_rms[peak_idx] * self.config.scoring.rms_weight +
                    (keyword_score / 10) * self.config.scoring.keyword_weight # Normalización simple
                )
                
                if final_hype_score >= self.config.hype_score_threshold:
                    candidate_highlights.append({
                        "start": start_time,
                        "end": end_time,
                        "score": final_hype_score
                    })
                    logger.success(f"Candidato Confirmado! Score: {final_hype_score:.2f}, Tiempo: {start_time:.2f}s - {end_time:.2f}s")

            finally:
                # Limpiar el archivo de audio temporal del segmento
                if segment_audio_path and os.path.exists(segment_audio_path):
                    os.remove(segment_audio_path)

        logger.info(f"Se confirmaron {len(candidate_highlights)} highlights tras el análisis de palabras clave.")
        return sorted(candidate_highlights, key=lambda x: x['score'], reverse=True)