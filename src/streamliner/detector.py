# src/streamliner/detector.py

import asyncio
import os
import numpy as np
import soundfile as sf
from scipy.signal import find_peaks
from loguru import logger
from pathlib import Path
from typing import Optional

from .stt import Transcriber

from .config import (
    DetectionConfig,
)


class HighlightDetector:
    """
    Analiza un archivo de audio para detectar momentos de alta "emoción" o "hype".
    Versión optimizada: Primero busca picos de energía (RMS) y luego solo transcribe
    esos segmentos, ahorrando una enorme cantidad de tiempo de procesamiento.
    """

    def __init__(
        self,
        detection_config: DetectionConfig,
        transcriber: Transcriber,  # Recibe la instancia del Transcriber
    ):
        self.detection_config = detection_config
        self.transcriber = transcriber  # Almacena la instancia

        # ¡LA CORRECCIÓN ESTÁ AQUÍ!
        # Accede a 'keywords' directamente desde detection_config
        self.general_keywords = self.detection_config.keywords
        self.streamer_keywords_map = self.detection_config.streamer_keywords

        # Y también es probable que necesites inicializar estos atributos desde la configuración
        # para que no fallen en otros lugares del código si se accede directamente a ellos.
        self.hype_score_threshold = self.detection_config.hype_score_threshold
        self.rms_peak_threshold = self.detection_config.rms_peak_threshold
        self.clip_duration_seconds = self.detection_config.clip_duration_seconds
        # Si rms_weight y keyword_weight también se usan directamente en el detector
        # Fuera de la llamada _calculate_keyword_score, deberías inicializarlos aquí.
        # Por ahora, solo se usan en `find_highlights`, donde se acceden correctamente.

        logger.info(
            f"HighlightDetector inicializado con umbral de hype: {self.detection_config.hype_score_threshold}"
        )
        logger.debug(
            f"Palabras clave generales cargadas: {list(self.general_keywords.keys())}"
        )
        logger.debug(
            f"Palabras clave de streamers cargadas para: {list(self.streamer_keywords_map.keys())}"
        )

    async def _extract_audio_segment(
        self, main_audio_path: Path, start: float, end: float, temp_dir: Path
    ) -> Optional[Path]:
        """
        Extrae un pequeño segmento del archivo de audio principal a un archivo temporal.
        Guarda el segmento en el 'temp_dir' especificado.
        """
        segment_path = temp_dir / f"temp_segment_{start:.0f}_{end:.0f}.wav"
        args = [
            "ffmpeg",
            "-y",
            "-i",
            str(main_audio_path),
            "-ss",
            str(start),
            "-to",
            str(end),
            "-c",
            "copy",
            str(segment_path),
        ]
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            logger.error(
                f"No se pudo extraer el segmento de audio: {segment_path}. "
                f"FFmpeg Error: {stderr.decode().strip()}"
            )
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

        rms_values = [
            np.sqrt(np.mean(audio_data[i * window_size : (i + 1) * window_size] ** 2))
            for i in range(num_windows)
        ]
        return np.array(rms_values)

    async def find_highlights(
        self,
        audio_path: Path,
        video_duration_sec: float,
        streamer_name: str,
        temp_dir: Path,  # temp_dir ahora es un argumento necesario
    ) -> list[dict]:  # Devuelve una lista de highlights, cada uno con su transcripción
        logger.info(
            f"Iniciando detección de highlights (Modo Eficiente) para {streamer_name}..."
        )

        # --- PASO 1: Análisis Rápido de Energía en todo el audio ---
        rms_scores = await self._calculate_rms(str(audio_path))
        if rms_scores.size == 0:
            logger.warning("No se pudo calcular el score RMS. Abortando detección.")
            return []

        rms_min, rms_max = np.min(rms_scores), np.max(rms_scores)
        if rms_max - rms_min < 1e-6:
            logger.warning("Audio sin variación de energía significativa.")
            return []

        normalized_rms = (rms_scores - rms_min) / (rms_max - rms_min)

        # --- PASO 2: Encontrar Picos de Energía (Candidatos a Highlight) ---
        candidate_peaks, _ = find_peaks(
            normalized_rms,
            height=self.detection_config.rms_peak_threshold,
            distance=self.detection_config.clip_duration_seconds,
        )

        if not candidate_peaks.any():
            logger.warning("No se encontraron picos de energía que superen el umbral.")
            return []

        logger.info(
            f"Se encontraron {len(candidate_peaks)} candidatos a highlight basados en la energía del audio."
        )

        # --- PASO 3: Análisis Enfocado - Transcribir solo los segmentos candidatos ---
        candidate_highlights = []
        for peak_idx in candidate_peaks:
            center_timestamp = peak_idx
            start_time = max(
                0, center_timestamp - self.detection_config.clip_duration_seconds / 2
            )
            end_time = min(
                video_duration_sec,
                center_timestamp + self.detection_config.clip_duration_seconds / 2,
            )

            segment_audio_path: Optional[Path] = None
            try:
                segment_audio_path = await self._extract_audio_segment(
                    audio_path,
                    start_time,
                    end_time,
                    temp_dir,  # Pasa temp_dir aquí
                )
                if not segment_audio_path:
                    continue

                # El método `transcribe` del Transcriber devuelve un diccionario
                transcription = await self.transcriber.transcribe(segment_audio_path)
                segment_text = transcription.text
                logger.debug(f"Texto del segmento transcrito: '{segment_text}'")

                keyword_score = self._calculate_keyword_score(
                    segment_text, streamer_name
                )

                final_hype_score = (
                    normalized_rms[peak_idx] * self.detection_config.scoring.rms_weight
                    + keyword_score * self.detection_config.scoring.keyword_weight
                )

                if final_hype_score >= self.detection_config.hype_score_threshold:
                    candidate_highlights.append(
                        {
                            "start_time": start_time,
                            "end_time": end_time,
                            "transcription": transcription,  # <-- DEVOLVER TRANSCRIPCIÓN
                            "score": final_hype_score,
                            "text": segment_text,
                        }
                    )
                    logger.success(
                        f"Candidato Confirmado! Score: {final_hype_score:.2f}, Tiempo: {start_time:.2f}s - {end_time:.2f}s, Keywords: {keyword_score:.2f}"
                    )

            finally:
                if segment_audio_path and segment_audio_path.exists():
                    try:
                        os.remove(segment_audio_path)
                        logger.debug(
                            f"Limpiado segmento de audio temporal: {segment_audio_path.name}"
                        )
                    except OSError as e:
                        logger.warning(
                            f"No se pudo eliminar el archivo temporal {segment_audio_path.name}: {e}"
                        )

        logger.info(
            f"Se confirmaron {len(candidate_highlights)} highlights tras el análisis de palabras clave para {streamer_name}."
        )
        return sorted(candidate_highlights, key=lambda x: x["score"], reverse=True)[
            : self.detection_config.max_clips_per_vod
        ]

    def _calculate_keyword_score(self, text_segment: str, streamer_name: str) -> float:
        score = 0.0
        current_streamer_specific_keywords = self.streamer_keywords_map.get(
            streamer_name, {}
        )
        combined_keywords = {
            **self.general_keywords,
            **current_streamer_specific_keywords,
        }
        logger.debug(
            f"Palabras clave combinadas para {streamer_name}: {list(combined_keywords.keys())}"
        )
        for keyword, weight in combined_keywords.items():
            if keyword.lower() in text_segment.lower():
                score += weight
                logger.debug(
                    f"Keyword '{keyword}' encontrada en '{text_segment}'. Score +{weight} = {score}"
                )
        return score
