# src/streamliner/pipeline.py
import asyncio
import os
from pathlib import Path
from loguru import logger
from typing import Optional, List
import subprocess
import aiofiles  # <--- IMPORT AIOFILES


from .config import AppConfig
from .stt import Transcriber
from .cutter import VideoCutter
from .render import VideoRenderer
from .detector import HighlightDetector
from .publisher.tiktok import TikTokPublisher
from .storage import get_storage


# ==============================================================================
# FUNCIÓN PARA EL MODO DE PROCESAMIENTO MANUAL (VODs COMPLETOS)
# Esta función es llamada por el comando `cli.py process --file ...`
# ==============================================================================
async def process_single_file(
    config: AppConfig, video_path_str: str, streamer_name: str, dry_run: bool = False
) -> None:
    """
    Procesa un solo archivo VOD para encontrar, cortar, transcribir, renderizar
    y opcionalmente subir highlights.
    """
    video_path = Path(video_path_str)
    logger.info(f"--- Iniciando pipeline de VOD para: {video_path} ---")

    # Inicializar componentes (alineado con la firma actual)
    transcriber = Transcriber(
        whisper_model=config.transcription.whisper_model,
        device=config.transcription.device,
        compute_type=config.transcription.compute_type,
        data_dir=config.paths.transcriber_models_dir,
    )
    detector = HighlightDetector(config.detection, transcriber)
    cutter = VideoCutter(config.paths.clips_dir)  # Cutter para cortar del VOD original

    # Variables para almacenar rutas de archivos a limpiar
    audio_to_clean: Optional[Path] = None
    cut_clips_to_clean: List[Path] = []

    try:
        # 1. Extraer audio completo del VOD
        # Usamos config.paths.data_dir como temp_dir para el audio del VOD
        audio_to_clean = await _extract_audio(video_path, config.paths.data_dir)

        # 2. Detectar highlights
        highlights = await detector.find_highlights(
            audio_path=audio_to_clean,
            video_duration_sec=await _get_video_duration(video_path),
            streamer_name=streamer_name,
            temp_dir=config.paths.data_dir,  # Directorio temporal para archivos del detector
        )

        if not highlights:
            logger.info("No se encontraron highlights en el VOD. Saliendo.")
            return

        for i, highlight in enumerate(highlights):
            logger.info(
                f"--- Procesando Highlight #{i + 1}/{len(highlights)} (Score: {highlight['score']:.2f}) ---"
            )

            # 3. Cortar el video original para obtener el clip
            # El nombre del archivo cortado usará el nombre del VOD original
            output_filename_str = f"{video_path.stem}_highlight_{i + 1}.mp4"
            cut_clip_path = await cutter.cut_clip(
                video_path,  # VOD completo como input
                start_time=highlight[
                    "start_time"
                ],  # Asegúrate de que detector devuelva 'start_time' y 'end_time'
                end_time=highlight[
                    "end_time"
                ],  # en lugar de 'start' y 'end'. Esto es una suposición.
                output_filename=output_filename_str,
            )
            cut_clips_to_clean.append(cut_clip_path)

            # 4. Procesar y crear el clip final (transcripción, renderizado, subida)
            # Pasamos el clip_path ya cortado, y el start_time/end_time no son necesarios aquí
            # Crear un publisher si la sección está habilitada
            publisher = (
                TikTokPublisher(config, get_storage(config))
                if config.publishing
                else None
            )
            rendered_clip_path = await process_and_create_clip(
                config,
                transcriber,
                publisher,
                cut_clip_path,  # Aquí, video_input es un solo Path
                streamer_name,
                dry_run=dry_run,
                temp_dir=config.paths.data_dir,  # Usar el directorio de datos para temporales
            )

            if rendered_clip_path:
                logger.success(
                    f"Highlight {i + 1} procesado y guardado en: {rendered_clip_path}"
                )
            else:
                logger.warning(
                    f"Highlight {i + 1} procesado, pero no se generó un clip renderizado (¿error?)."
                )

    finally:
        logger.info("Iniciando limpieza de archivos temporales...")
        if audio_to_clean and audio_to_clean.exists():
            await try_delete(audio_to_clean)
            logger.debug(f"Limpiado audio original: {audio_to_clean.name}")

        for clip_path_to_clean in cut_clips_to_clean:
            if clip_path_to_clean.exists():
                await try_delete(clip_path_to_clean)
                logger.debug(
                    f"Limpiado clip cortado intermedio: {clip_path_to_clean.name}"
                )

        logger.info("Limpieza de archivos temporales completada.")


# ==============================================================================
# FUNCIÓN PARA EL MODO DE TIEMPO REAL (CHUNKS) O CLIPS CORTADOS
# Esta función es llamada por el `worker.py` o por `process_single_file`
# ==============================================================================
async def process_and_create_clip(
    config: AppConfig,
    transcriber: Transcriber,  # Recibir instancia
    publisher: Optional[TikTokPublisher],  # Recibir instancia
    video_input: Path,  # Ahora siempre es un Path (el video combinado o un VOD)
    streamer_name: str,
    # Argumentos opcionales para optimización
    transcription_result: Optional[dict] = None,
    # Añadimos buffer_start_absolute_time para el contexto de los chunks
    buffer_start_absolute_time: float = 0.0,
    highlight_start_abs: float = 0.0,  # Tiempo de inicio ABSOLUTO del highlight
    highlight_end_abs: float = -1.0,  # Tiempo de fin ABSOLUTO del highlight
    dry_run: bool = False,
    temp_dir: Optional[
        Path
    ] = None,  # Directorio temporal para los archivos intermedios
) -> Optional[Path]:
    """Procesa una entrada de video (un Path de video)
    para transcribir, renderizar y opcionalmente subir un clip.
    Si se proporciona una lista de chunks, cortará el highlight del rango de tiempo especificado.
    """
    if temp_dir is None:
        temp_dir = config.paths.data_dir  # Usar el directorio de datos por defecto

    logger.info(f"--- Iniciando pipeline de creación de clip para {streamer_name} ---")

    renderer = VideoRenderer(config.rendering)
    # Cutter para cortar de chunks combinados (usará temp_dir como output)
    cutter_for_chunks = VideoCutter(temp_dir)

    clip_source_path: (
        Path  # El path del video del que extraeremos audio y renderizaremos
    )
    clip_audio_path: Optional[Path] = None
    vtt_output_path: Optional[Path] = None
    rendered_clip_path: Optional[Path] = None

    # Lista para limpiar archivos intermedios generados en esta función
    files_to_clean_locally: List[Path] = []

    try:
        # Si highlight_end_abs es > 0, significa que estamos en modo de tiempo real
        # y necesitamos cortar el highlight del video combinado (video_input).
        if highlight_end_abs > 0:
            logger.info(
                f"Cortando highlight de video combinado. Rango ABSOLUTO: {highlight_start_abs:.2f}s - {highlight_end_abs:.2f}s"
            )
            temp_cut_highlight_filename = f"{streamer_name}_highlight_{int(highlight_start_abs)}_{int(highlight_end_abs)}.mp4"
            clip_source_path = temp_dir / temp_cut_highlight_filename

            # Calcular tiempos relativos para el corte
            cut_start_relative = highlight_start_abs - buffer_start_absolute_time
            cut_end_relative = highlight_end_abs - buffer_start_absolute_time

            # Cortar el clip
            clip_source_path = await cutter_for_chunks.cut_clip(
                input_path=video_input,  # El video combinado
                start_time=cut_start_relative,
                end_time=cut_end_relative,
                output_filename=temp_cut_highlight_filename,
            )
            files_to_clean_locally.append(clip_source_path)
        else:
            # Modo VOD o clip pre-cortado: usar el Path directamente
            clip_source_path = video_input
            logger.info(f"Usando clip de entrada: {clip_source_path.name}")

        # 3. Generar subtítulos: VTT clásico o ASS karaoke según config
        subtitle_mode = getattr(config.rendering, "subtitle_mode", "plain")
        subtitle_path: Path
        if subtitle_mode == "karaoke":
            ass_output_filename = clip_source_path.stem + ".ass"
            subtitle_path = temp_dir / ass_output_filename
            # Construir set de keywords (globales + del streamer si existen)
            keywords_set: set[str] = set(
                map(str.lower, config.detection.keywords.keys())
            )
            streamer_kw = config.detection.streamer_keywords.get(streamer_name)
            if streamer_kw:
                keywords_set.update(map(str.lower, streamer_kw.keys()))
            await transcriber.save_transcription_to_ass_karaoke(
                output_path=subtitle_path,
                transcription_result=transcription_result,
                audio_path=clip_source_path if not transcription_result else None,
                clip_start_offset=0.0,
                font_size=config.rendering.subtitle_font_size,
                margin_v=config.rendering.subtitle_safe_margin_v,
                color_base=getattr(
                    config.rendering, "subtitle_color_base", "&H00FFFFFF"
                ),
                color_highlight=getattr(
                    config.rendering, "subtitle_color_highlight", "&H0000FFFF"
                ),
                keywords=keywords_set,
            )
        else:
            vtt_output_filename = clip_source_path.stem + ".vtt"
            subtitle_path = temp_dir / vtt_output_filename
            await transcriber.save_transcription_to_vtt(
                output_path=subtitle_path,
                transcription_result=transcription_result,  # Usar la transcripción pre-hecha
                audio_path=clip_source_path if not transcription_result else None,
                clip_start_offset=0.0,
                max_lines_per_cue=config.rendering.subtitle_max_lines_per_cue,
                max_chars_per_line=config.rendering.subtitle_max_chars_per_line,
            )
        files_to_clean_locally.append(subtitle_path)

        # 4. Renderizar el clip con subtítulos y logo
        # El nombre del archivo final debe ser único, especialmente si se genera en tiempo real
        final_clip_filename = f"{streamer_name}_{clip_source_path.stem}_rendered.mp4"
        final_clip_path = config.paths.clips_dir / final_clip_filename

        logger.info(f"Renderizando clip final: {final_clip_path.name}")
        rendered_clip_path = await renderer.render_vertical_clip(
            input_path=clip_source_path,
            output_path=final_clip_path,
            srt_path=subtitle_path,
        )

        # Si estamos en dry_run, hemos renderizado, pero NO subimos.
        if dry_run:
            logger.info(
                f"Modo DRY-RUN: El clip renderizado '{rendered_clip_path.name}' "
                f"ha sido creado en el disco, pero NO se subirá a TikTok."
            )
            return rendered_clip_path  # Retorna el path del clip renderizado, saltando la subida.

        # 5. Opcional: Subir el clip (si la sección 'publishing' está configurada en config)
        if publisher and rendered_clip_path:
            logger.info(f"Subiendo clip {rendered_clip_path.name} a TikTok...")
            try:
                upload_success = await publisher.upload_clip(
                    str(rendered_clip_path), streamer_name, dry_run=dry_run
                )

                if upload_success:
                    logger.success(
                        f"Clip {rendered_clip_path.name} subido exitosamente a TikTok."
                    )
                else:
                    logger.error(
                        f"Fallo al subir el clip {rendered_clip_path.name} a TikTok."
                    )

            except ImportError:
                logger.error(
                    "TikTokPublisher no pudo ser importado. Asegúrate de que el módulo existe y las dependencias están instaladas."
                )
            except Exception as e:
                logger.error(
                    f"Error inesperado al intentar subir el clip a TikTok: {e}",
                    exc_info=True,
                )

        logger.success(
            f"Pipeline de creación de clip completado para: {clip_source_path.name}. "
            f"Clip final en: {rendered_clip_path.name if rendered_clip_path else 'N/A'}"
        )
        return rendered_clip_path

    finally:
        logger.info(
            f"Iniciando limpieza de archivos temporales para clip: {clip_source_path.name if 'clip_source_path' in locals() else 'N/A'}..."
        )
        for f in files_to_clean_locally:
            if f.exists():
                await try_delete(f)
                logger.debug(f"Limpiado archivo temporal: {f.name}")

        logger.info(
            f"Limpieza de archivos temporales para clip '{clip_source_path.name if 'clip_source_path' in locals() else 'N/A'}' completada."
        )


# --- Nuevas Funciones de Ayuda para el Procesamiento en Tiempo Real ---


async def _cut_clip_from_chunks(
    chunk_paths: List[Path],
    absolute_start_time: float,  # Tiempo ABSOLUTO del inicio del highlight
    absolute_end_time: float,  # Tiempo ABSOLUTO del fin del highlight
    buffer_start_absolute_time: float,  # Tiempo ABSOLUTO del inicio del primer chunk en chunk_paths
    output_path: Path,
    temp_dir: Path,
) -> bool:
    """
    Une los chunks relevantes del stream y corta un highlight basado en tiempos absolutos.
    Requiere el tiempo absoluto de inicio del buffer de chunks para calcular offsets relativos.
    """
    logger.info(
        f"Preparando para cortar highlight de chunks entre {absolute_start_time:.2f}s y {absolute_end_time:.2f}s. "
        f"Buffer inicia en: {buffer_start_absolute_time:.2f}s. Salida: {output_path.name}"
    )

    # Las variables chunk_duration y relevant_chunks ya no son necesarias aquí con el nuevo enfoque.
    # Eliminadas para resolver F841.

    combined_input_path = temp_dir / f"{chunk_paths[0].stem}_combined_for_clip.mp4"
    list_file_path = temp_dir / f"{chunk_paths[0].stem}_clip_concat_list.txt"

    try:
        # 1. Crear el archivo de lista para FFmpeg
        async with aiofiles.open(list_file_path, "w") as f:
            for chunk_path in chunk_paths:
                await f.write(
                    f"file '{chunk_path.name}'\n"
                )  # Usar .name porque ffmpeg se ejecutará en temp_dir

        # 2. Unir los chunks en un archivo temporal
        command_concat = [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            list_file_path.name,  # <-- CORRECCIÓN: Usar solo el nombre del archivo
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            combined_input_path.name,
        ]
        process_concat = await asyncio.create_subprocess_exec(
            *command_concat,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(
                temp_dir
            ),  # Importante: ejecutar en el directorio donde están los chunks y el list_file
        )
        stdout_concat, stderr_concat = await process_concat.communicate()
        if process_concat.returncode != 0:
            logger.error(
                f"FFmpeg error al concatenar chunks para corte: {stderr_concat.decode().strip()}"
            )
            return False
        logger.debug(f"Chunks combinados temporalmente en {combined_input_path.name}")

        # 3. Calcular los tiempos relativos para el corte de FFmpeg
        # Estos son los tiempos que _VideoCutter_ necesita, relativos al inicio de _combined_input_path_.
        cut_start_relative = absolute_start_time - buffer_start_absolute_time
        cut_end_relative = absolute_end_time - buffer_start_absolute_time

        # Asegurarse de que los tiempos no sean negativos (en caso de algún desajuste)
        cut_start_relative = max(0.0, cut_start_relative)
        # Asegurarse de que el tiempo de fin no sea menor que el de inicio si los datos son raros
        cut_end_relative = max(
            cut_start_relative + 0.1, cut_end_relative
        )  # Asegurar una duración mínima

        logger.debug(
            f"Tiempos de corte RELATIVOS al video combinado: {cut_start_relative:.2f}s - {cut_end_relative:.2f}s"
        )

        # Ahora, cortar el highlight del video combinado
        cutter = VideoCutter(
            output_path.parent
        )  # El VideoCutter necesita un output_dir (que es temp_dir aquí)

        cut_video_path = await cutter.cut_clip(
            combined_input_path,
            start_time=cut_start_relative,
            end_time=cut_end_relative,
            output_filename=output_path.name,  # El VideoCutter construirá el Path completo
        )

        if cut_video_path and cut_video_path.exists():
            # Renombrar el archivo al nombre final deseado si VideoCutter lo nombra diferente.
            # VideoCutter ya lo guarda con output_filename, así que solo verificamos.
            if cut_video_path != output_path:
                logger.warning(
                    f"VideoCutter guardó como {cut_video_path.name}, se esperaba {output_path.name}. "
                    "Renombrando..."
                )
                try:
                    os.rename(cut_video_path, output_path)
                    cut_video_path = output_path
                except OSError as e:
                    logger.error(f"Error al renombrar el clip cortado: {e}")
                    return False
            logger.success(
                f"Highlight cortado exitosamente de chunks a {output_path.name}"
            )
            return True
        else:
            logger.error(f"Fallo al cortar el highlight de {combined_input_path.name}")
            return False

    except Exception as e:
        logger.error(f"Error en _cut_clip_from_chunks: {e}", exc_info=True)
        return False
    finally:
        # Limpiar archivos temporales de concatenación
        if list_file_path.exists():
            await try_delete(list_file_path)
        if combined_input_path.exists():
            await try_delete(combined_input_path)


# --- Funciones de Ayuda ---


async def _extract_audio(video_path: Path, output_dir: Path) -> Path:
    """Extrae el audio de un video a un formato WAV y lo guarda en output_dir."""
    # Asegurarse de que el proceso de FFmpeg se libere correctamente
    # al esperar su finalización con process.communicate()
    audio_path = output_dir / f"{video_path.stem}.wav"
    logger.info(
        f"Extrayendo y convirtiendo audio de '{video_path.name}' a WAV en '{output_dir.name}'..."
    )
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
        raise RuntimeError(
            f"Fallo en la extracción de audio con ffmpeg para {video_path.name}."
        )
    logger.success(f"Extracción de audio a WAV completada: {audio_path.name}.")
    return audio_path


def _format_srt_time(seconds: float) -> str:
    """Convierte segundos a formato de tiempo SRT (HH:MM:SS,ms)."""
    millis = int((seconds % 1) * 1000)
    seconds = int(seconds)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


async def _get_video_duration(video_path: Path) -> float:
    """Obtiene la duración de un video en segundos usando ffprobe."""
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        logger.error(
            f"Error al obtener la duración del video {video_path}: {stderr.decode()}"
        )
        raise RuntimeError(f"FFprobe failed to get video duration: {stderr.decode()}")
    try:
        return float(stdout.decode().strip())
    except ValueError:
        logger.error(f"No se pudo parsear la duración del video: {stdout.decode()}")
        raise ValueError("Invalid video duration format from ffprobe.")


async def try_delete(path: Path, max_retries: int = 5, delay: float = 1.0):
    """
    Intenta eliminar un archivo de forma segura, con reintentos.
    Función asíncrona para no bloquear el bucle de eventos.
    """
    if not path.exists():
        return

    for attempt in range(max_retries):
        try:
            os.remove(path)
            logger.debug(f"Archivo temporal {path.name} eliminado con éxito.")
            return
        except PermissionError as e:
            if attempt < max_retries - 1:
                logger.warning(
                    f"No se pudo eliminar {path.name} (intento {attempt + 1}/{max_retries}): {e}. Reintentando en {delay}s..."
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    f"Fallo persistente al eliminar {path.name} después de {max_retries} intentos: {e}"
                )
        except Exception as e:
            logger.error(
                f"Error inesperado al intentar eliminar {path.name}: {e}", exc_info=True
            )
            return
