# src/streamliner/pipeline.py

import asyncio
import os
from pathlib import Path
from loguru import logger

from .config import AppConfig
from .stt import Transcriber
from .cutter import VideoCutter
from .render import VideoRenderer
from .publisher.tiktok import TikTokPublisher
from .storage import get_storage
from .detector import HighlightDetector  # <-- Importación restaurada


# ==============================================================================
# FUNCIÓN PARA EL MODO DE PROCESAMIENTO MANUAL (VODs COMPLETOS)
# Esta función es llamada por el comando `cli.py process --file ...`
# ==============================================================================
async def process_single_file(
    config: AppConfig, video_path_str: str, streamer: str, dry_run: bool
):
    """
    Orquesta el pipeline completo para un solo archivo de video VOD.
    Detecta -> Corta -> Procesa -> Publica.
    """
    logger.info(f"--- Iniciando pipeline de VOD para: {video_path_str} ---")
    video_path = Path(video_path_str)

    # 1. Detección de Highlights en el VOD completo
    audio_path = None
    try:
        audio_path = await _extract_audio(video_path)
        detector = HighlightDetector(config)
        # TODO: Obtener duración real del video dinámicamente
        video_duration_sec = 3600 * 2  # Asumimos 2 horas para el scoring
        highlights = await detector.find_highlights(str(audio_path), video_duration_sec)

        if not highlights:
            logger.warning(f"No se encontraron highlights en '{video_path.name}'.")
            return

        # 2. Procesamiento de cada Highlight encontrado
        clips_to_process = highlights[: config.detection.max_clips_per_vod]
        for i, highlight in enumerate(clips_to_process):
            logger.info(
                f"--- Procesando Highlight #{i + 1}/{len(clips_to_process)} (Score: {highlight['score']:.2f}) ---"
            )
            # Cortamos el clip del VOD original
            cutter = VideoCutter()
            clip_filename = (
                video_path.parent / f"{video_path.stem}_highlight_{i + 1}.mp4"
            )

            cut_clip_path_str = await cutter.cut_clip(
                video_path, clip_filename, highlight["start"], highlight["end"]
            )
            if not cut_clip_path_str:
                logger.error(
                    "Fallo al cortar el clip. Saltando al siguiente highlight."
                )
                continue

            # Ahora que tenemos el clip cortado, usamos la otra función para procesarlo
            await process_and_create_clip(
                config, Path(cut_clip_path_str), streamer, dry_run
            )

            # Limpiamos el clip cortado intermedio
            if os.path.exists(cut_clip_path_str):
                os.remove(cut_clip_path_str)

    finally:
        if audio_path and os.path.exists(audio_path):
            os.remove(audio_path)


# ==============================================================================
# FUNCIÓN PARA EL MODO DE TIEMPO REAL (CHUNKS)
# Esta función es llamada por el `worker.py`
# ==============================================================================
async def process_and_create_clip(
    config: AppConfig,
    video_path: Path,  # Aquí, video_path es un chunk o un clip ya cortado
    streamer: str,
    dry_run: bool = False,
):
    """
    Toma un archivo de video corto (chunk/clip), lo renderiza y publica.
    """
    logger.info(
        f"--- Iniciando pipeline de creación de clip para: {video_path.name} ---"
    )
    storage = get_storage(config)
    temp_files = []

    try:
        # Transcribir el Clip para Subtítulos
        transcriber = Transcriber(config.transcription)
        clip_audio_path = await _extract_audio(video_path)
        temp_files.append(clip_audio_path)
        transcription = await transcriber.transcribe(clip_audio_path)

        srt_path = video_path.with_suffix(".srt")
        temp_files.append(srt_path)

        with open(srt_path, "w", encoding="utf-8") as srt_file:
            line_number = 1
            for segment in transcription.get("segments", []):
                start, end, text = segment["start"], segment["end"], segment["text"]
                srt_file.write(f"{line_number}\n")
                srt_file.write(
                    f"{_format_srt_time(start)} --> {_format_srt_time(end)}\n"
                )
                srt_file.write(f"{text.strip()}\n\n")
                line_number += 1

        # Renderizar el Clip Vertical
        renderer = VideoRenderer(config)
        final_clip_dir = Path(config.downloader.local_storage_path) / "clips_generados"
        final_clip_dir.mkdir(parents=True, exist_ok=True)
        final_clip_path = final_clip_dir / f"{video_path.stem}_final.mp4"

        await renderer.render_vertical_clip(
            str(video_path), str(final_clip_path), str(srt_path)
        )

        # Publicar el Clip
        publisher = TikTokPublisher(config, storage)
        if config.storage.storage_type in ["s3", "r2"]:
            remote_key = await storage.upload(final_clip_path, final_clip_path.name)
            await publisher.upload_clip(remote_key, streamer, dry_run=dry_run)
        else:
            await publisher.upload_clip(str(final_clip_path), streamer, dry_run=dry_run)

    finally:
        logger.debug(f"Limpiando archivos temporales para {video_path.name}...")
        for f in temp_files:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except OSError as e:
                    logger.warning(f"No se pudo eliminar el archivo temporal {f}: {e}")


# --- Funciones de Ayuda ---


async def _extract_audio(video_path: Path) -> Path:
    """Extrae el audio de un video a un formato WAV."""
    audio_path = video_path.with_suffix(".wav")
    logger.info(f"Extrayendo y convirtiendo audio de '{video_path.name}' a WAV...")
    args = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        str(audio_path),
    ]
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        logger.error(f"Error al extraer audio: {stderr.decode()}")
        raise RuntimeError("Fallo en la extracción de audio con ffmpeg.")
    logger.success("Extracción de audio a WAV completada.")
    return audio_path


def _format_srt_time(seconds: float) -> str:
    """Convierte segundos a formato de tiempo SRT (HH:MM:SS,ms)."""
    millis = int((seconds % 1) * 1000)
    seconds = int(seconds)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"
