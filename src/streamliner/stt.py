# src/streamliner/stt.py

import asyncio
from pathlib import Path
from faster_whisper import WhisperModel
from loguru import logger
from .config import TranscriptionConfig


class Transcriber:
    """
    Un wrapper alrededor de faster-whisper para realizar la transcripción de audio.
    Carga el modelo una vez y lo reutiliza para múltiples transcripciones.
    """

    def __init__(self, config: TranscriptionConfig):
        self.config = config
        logger.info(
            f"Cargando modelo de Whisper '{config.whisper_model}' en '{config.device}'..."
        )

        try:
            self.model = WhisperModel(
                config.whisper_model,
                device=config.device,
                compute_type=config.compute_type,
            )
            logger.success("Modelo de Whisper cargado exitosamente.")
        except Exception as e:
            logger.error(f"No se pudo cargar el modelo de Whisper: {e}")
            logger.error(
                "Asegúrate de tener las dependencias correctas para tu hardware (CPU/GPU)."
            )
            # En un caso real, podríamos querer salir del programa si el modelo no carga.
            raise

    async def transcribe(self, audio_path: str | Path) -> dict:
        """
        Transcribe un archivo de audio y devuelve el resultado con segmentos y timestamps.

        Aunque este método es 'async', la librería 'faster-whisper' es bloqueante (CPU-bound).
        Lo ejecutamos en un hilo aparte usando asyncio.to_thread para no bloquear el
        event loop principal de asyncio.
        """
        logger.info(f"Iniciando transcripción para: {audio_path}")

        def sync_transcribe():
            """Función síncrona que se ejecutará en el hilo secundario."""
            segments, info = self.model.transcribe(
                str(audio_path),
                beam_size=5,
                word_timestamps=True,  # Importante para análisis futuros
            )

            logger.info(
                f"Lenguaje detectado: '{info.language}' con probabilidad {info.language_probability:.2f}"
            )

            # El generador 'segments' se consume aquí para convertirlo en una lista
            segment_list = list(segments)

            # Reconstruimos el texto completo
            full_text = " ".join([s.text.strip() for s in segment_list])

            return {"text": full_text, "segments": [s._asdict() for s in segment_list]}

        try:
            # Ejecutamos la función bloqueante en el pool de hilos por defecto de asyncio
            result = await asyncio.to_thread(sync_transcribe)
            logger.success(f"Transcripción de {audio_path} completada.")
            return result
        except Exception as e:
            logger.error(f"Ocurrió un error durante la transcripción: {e}")
            return {"text": "", "segments": []}
