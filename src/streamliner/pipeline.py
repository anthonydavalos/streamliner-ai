# src/streamliner/pipeline.py

import asyncio
import os
from pathlib import Path
from loguru import logger

from .config import AppConfig
from .detector import HighlightDetector
from .stt import Transcriber
from .cutter import VideoCutter
from .render import VideoRenderer
from .publisher.tiktok import TikTokPublisher
from .storage import get_storage

async def _extract_audio(video_path: Path) -> Path:
    """
    Extrae el audio de un video y lo convierte a un formato WAV estándar
    para máxima compatibilidad con librerías de análisis de audio.
    """
    # El archivo de salida ahora será .wav
    audio_path = video_path.with_suffix(".wav")
    logger.info(f"Extrayendo y convirtiendo audio de '{video_path}' a '{audio_path}' (formato WAV)...")

    # Comando ffmpeg mejorado para convertir a WAV
    args = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn",                      # No video
        "-acodec", "pcm_s16le",     # Formato estándar para WAV (16-bit)
        "-ar", "16000",             # Frecuencia de muestreo de 16kHz (suficiente para voz)
        "-ac", "1",                 # Convertir a mono (un solo canal)
        str(audio_path)
    ]

    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        logger.error(f"Error al extraer audio: {stderr.decode()}")
        raise RuntimeError("Fallo en la extracción de audio con ffmpeg.")

    logger.success("Extracción y conversión de audio a WAV completada.")
    return audio_path

async def process_single_file(config: AppConfig, video_path_str: str, streamer: str, dry_run: bool):
    """
    Orquesta el pipeline completo para un solo archivo de video.
    Detecta -> Corta -> Transcribe -> Renderiza -> Publica.
    """
    video_path = Path(video_path_str)
    storage = get_storage(config)

    # --- 1. Detección de Highlights ---
    audio_path = None
    try:
        audio_path = await _extract_audio(video_path)
        detector = HighlightDetector(config)
        # TODO: Obtener la duración real del video en lugar de hardcodearla.
        # Por ahora, asumimos que los VODs son largos y los clips cortos.
        video_duration_sec = 3600 * 2 # Asumimos 2 horas para el scoring
        highlights = await detector.find_highlights(str(audio_path), video_duration_sec)

        if not highlights:
            logger.warning(f"No se encontraron highlights en '{video_path.name}'. Finalizando proceso.")
            return

        # --- 2. Procesamiento de cada Highlight ---
        # Limitamos la cantidad de clips a procesar si está configurado
        clips_to_process = highlights[:config.detection.max_clips_per_vod]
        
        for i, highlight in enumerate(clips_to_process):
            logger.info(f"--- Procesando Highlight #{i+1}/{len(clips_to_process)} (Score: {highlight['score']:.2f}) ---")
            
            temp_files = []
            try:
                # --- 2a. Cortar el Clip ---
                cutter = VideoCutter()
                clip_filename = video_path.parent / f"{video_path.stem}_clip_{i+1}.mp4"
                temp_files.append(clip_filename)
                
                cut_clip_path_str = await cutter.cut_clip(
                    video_path, clip_filename, highlight["start"], highlight["end"]
                )
                if not cut_clip_path_str:
                    logger.error("Fallo al cortar el clip. Saltando al siguiente highlight.")
                    continue
                
                cut_clip_path = Path(cut_clip_path_str)

                # --- 2b. Transcribir el Clip para Subtítulos ---
                transcriber = Transcriber(config.transcription)
                clip_audio_path = await _extract_audio(cut_clip_path)
                temp_files.append(clip_audio_path)

                transcription = await transcriber.transcribe(clip_audio_path)
                
                # Generar archivo SRT
                srt_path = cut_clip_path.with_suffix(".srt")
                temp_files.append(srt_path)
                with open(srt_path, "w", encoding="utf-8") as srt_file:
                    for segment in transcription['segments']:
                        start = segment['start']
                        end = segment['end']
                        text = segment['text']
                        # Formato SRT
                        srt_file.write(f"{len(temp_files)}\n")
                        srt_file.write(f"{int(start//3600):02}:{int(start%3600//60):02}:{int(start%60):02},{int(start%1*1000):03} --> ")
                        srt_file.write(f"{int(end//3600):02}:{int(end%3600//60):02}:{int(end%60):02},{int(end%1*1000):03}\n")
                        srt_file.write(f"{text.strip()}\n\n")

                # --- 2c. Renderizar el Clip Vertical ---
                renderer = VideoRenderer(config)
                final_clip_path = video_path.parent / f"{video_path.stem}_final_{i+1}.mp4"
                #temp_files.append(final_clip_path)

                await renderer.render_vertical_clip(str(cut_clip_path), str(final_clip_path), str(srt_path))

                # --- 2d. Publicar el Clip ---
                publisher = TikTokPublisher(config, storage)
                # Si es S3/R2, primero subimos el clip final para obtener una URL pública
                if config.storage.storage_type in ["s3", "r2"]:
                    remote_key = await storage.upload(final_clip_path, final_clip_path.name)
                    await publisher.upload_clip(remote_key, streamer, dry_run=dry_run)
                else: # Si es local, pasamos la ruta directamente (aunque PULL_FROM_URL no funcionará)
                    await publisher.upload_clip(str(final_clip_path), streamer, dry_run=dry_run)

            finally:
                # --- 2e. Limpieza de archivos temporales del clip ---
                logger.debug("Limpiando archivos temporales del clip...")
                for f in temp_files:
                    try:
                        if os.path.exists(f):
                            os.remove(f)
                    except OSError as e:
                        logger.warning(f"No se pudo eliminar el archivo temporal {f}: {e}")

    finally:
        # --- 3. Limpieza final ---
        logger.debug("Limpiando archivo de audio principal...")
        if audio_path and os.path.exists(audio_path):
            try:
                os.remove(audio_path)
            except OSError as e:
                logger.warning(f"No se pudo eliminar el archivo de audio principal {audio_path}: {e}")