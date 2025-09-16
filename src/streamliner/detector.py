import asyncio
import os
import numpy as np
import soundfile as sf
from scipy.signal import find_peaks
from loguru import logger
from pathlib import Path

from .stt import Transcriber
from .config import AppConfig  # Importa AppConfig y DetectionConfig


class HighlightDetector:
    """
    Analiza un archivo de audio para detectar momentos de alta "emoción" o "hype".
    Versión optimizada: Primero busca picos de energía (RMS) y luego solo transcribe
    esos segmentos, ahorrando una enorme cantidad de tiempo de procesamiento.
    """

    # MODIFICACIÓN 1: El constructor ahora recibe AppConfig completa
    def __init__(self, config: AppConfig):
        self.config = config.detection  # Almacena solo la DetectionConfig
        self.transcriber = Transcriber(
            config.transcription
        )  # Inicializa el Transcriber

        # NUEVAS LÍNEAS: Cargar palabras clave generales y específicas del streamer
        self.general_keywords = self.config.keywords
        self.streamer_keywords_map = self.config.streamer_keywords

        logger.info(
            f"HighlightDetector inicializado con umbral de hype: {self.config.hype_score_threshold}"
        )
        logger.debug(
            f"Palabras clave generales cargadas: {list(self.general_keywords.keys())}"
        )
        logger.debug(
            f"Palabras clave de streamers cargadas para: {list(self.streamer_keywords_map.keys())}"
        )

    async def _extract_audio_segment(
        self, main_audio_path: Path, start: float, end: float
    ) -> Path:
        """Extrae un pequeño segmento del archivo de audio principal a un archivo temporal."""
        segment_path = (
            main_audio_path.parent / f"temp_segment_{start:.0f}_{end:.0f}.wav"
        )
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
        stdout, stderr = await process.communicate()  # Capturamos stdout y stderr
        if process.returncode != 0:
            logger.error(
                f"No se pudo extraer el segmento de audio: {segment_path}. "
                f"FFmpeg Error: {stderr.decode().strip()}"  # Añadimos el error de ffmpeg
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

    # MODIFICACIÓN 2: find_highlights ahora acepta streamer_name
    async def find_highlights(
        self,
        audio_path_str: str,
        video_duration_sec: float,
        streamer_name: str,  # NUEVO: streamer_name
    ) -> list[dict]:
        logger.info(
            f"Iniciando detección de highlights (Modo Eficiente) para {streamer_name}..."
        )
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
        candidate_peaks, _ = find_peaks(
            normalized_rms,
            height=self.config.rms_peak_threshold,
            distance=self.config.clip_duration_seconds,
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
                0, center_timestamp - self.config.clip_duration_seconds / 2
            )
            end_time = min(
                video_duration_sec,
                center_timestamp + self.config.clip_duration_seconds / 2,
            )

            segment_audio_path = None
            try:
                segment_audio_path = await self._extract_audio_segment(
                    audio_path, start_time, end_time
                )
                if not segment_audio_path:
                    continue

                transcription = await self.transcriber.transcribe(segment_audio_path)
                segment_text = transcription.get(
                    "text", ""
                )  # Obtener el texto del segmento
                logger.debug(f"Texto del segmento transcrito: '{segment_text}'")

                # MODIFICACIÓN 3: Usar la nueva función para calcular el score de palabras clave
                keyword_score = self._calculate_keyword_score(
                    segment_text, streamer_name
                )

                final_hype_score = (
                    normalized_rms[peak_idx] * self.config.scoring.rms_weight
                    + keyword_score * self.config.scoring.keyword_weight
                    # Si implementas scene_change, iría aquí sumando su peso
                )

                if final_hype_score >= self.config.hype_score_threshold:
                    candidate_highlights.append(
                        {
                            "start": start_time,
                            "end": end_time,
                            "score": final_hype_score,
                            "text": segment_text,  # Guardar el texto transcrito del highlight
                        }
                    )
                    logger.success(
                        f"Candidato Confirmado! Score: {final_hype_score:.2f}, Tiempo: {start_time:.2f}s - {end_time:.2f}s, Keywords: {keyword_score:.2f}"
                    )

            finally:
                if segment_audio_path and os.path.exists(segment_audio_path):
                    os.remove(segment_audio_path)

        logger.info(
            f"Se confirmaron {len(candidate_highlights)} highlights tras el análisis de palabras clave para {streamer_name}."
        )
        return sorted(candidate_highlights, key=lambda x: x["score"], reverse=True)[
            : self.config.max_clips_per_vod
        ]  # Limita a max_clips_per_vod

    # NUEVA FUNCIÓN: _calculate_keyword_score
    def _calculate_keyword_score(self, text_segment: str, streamer_name: str) -> float:
        """
        Calcula la puntuación de palabras clave para un segmento de texto.
        Prioriza las palabras clave específicas del streamer sobre las generales.
        """
        score = 0.0

        # Obtener las palabras clave específicas del streamer actual
        current_streamer_specific_keywords = self.streamer_keywords_map.get(
            streamer_name, {}
        )

        # Fusionar las palabras clave: las del streamer tienen prioridad
        # Si una palabra está en ambas listas, el peso de streamer_keywords_map prevalece
        combined_keywords = {
            **self.general_keywords,
            **current_streamer_specific_keywords,
        }

        logger.debug(
            f"Palabras clave combinadas para {streamer_name}: {list(combined_keywords.keys())}"
        )

        for keyword, weight in combined_keywords.items():
            # Usar 'in' para que detecte la palabra dentro del texto
            if keyword.lower() in text_segment.lower():
                score += weight
                logger.debug(
                    f"Keyword '{keyword}' encontrada en '{text_segment}'. Score +{weight} = {score}"
                )
        return score
