# src/streamliner/stt.py (CORREGIDO)

import asyncio
from typing import Optional
from pathlib import Path
from loguru import logger
from faster_whisper import WhisperModel
import torch
from dataclasses import dataclass, field
import re

# No necesitamos importar TranscriptionConfig aquí si vamos a pasar los atributos individuales
# Si la necesitas para tipado en algún otro método, puedes mantenerla o definir un dataclass local si es solo para STT
# from .config import TranscriptionConfig # Puedes quitar esta línea si no la usas

# --- INICIO DE CÓDIGO DEPURACIÓN TEMPORAL (QUITAR DESPUÉS DE LA SOLUCIÓN) ---
# import inspect
# import sys
# logger.critical(f"DEBUG: stt.py is being loaded from: {__file__}")
# try:
#     if 'Transcriber' in globals():
#         init_signature = inspect.signature(Transcriber.__init__)
#         logger.critical(f"DEBUG: Transcriber.__init__ signature: {init_signature}")
#     else:
#         logger.critical("DEBUG: Transcriber class not yet defined at this point in stt.py")
# except Exception as e:
#     logger.critical(f"DEBUG: Error getting Transcriber.__init__ signature: {e}")
# --- FIN DE CÓDIGO DEPURACIÓN TEMPORAL ---


@dataclass
class TranscriptionResult:
    text: str
    segments: list[dict] = field(default_factory=list)
    language: str = ""  # Añadir el idioma si lo retorna el transcriptor


class Transcriber:
    # EL CONSTRUCTOR AHORA ACEPTA ARGUMENTOS INDIVIDUALES, INCLUIDO data_dir
    def __init__(
        self,
        whisper_model: str,
        device: str,
        compute_type: str,
        data_dir: Path,  # Renombrado para evitar confusión con config.data_dir
    ):
        self.whisper_model_name = whisper_model  # Guardamos el nombre del modelo
        self.initial_device = device  # Guardamos el dispositivo solicitado
        self.initial_compute_type = compute_type  # Guardamos el compute_type solicitado
        self.model_data_dir = (
            data_dir  # Este es el directorio para los archivos del modelo
        )

        # Determinar el dispositivo de forma robusta
        if self.initial_device == "cuda" and torch.cuda.is_available():
            self.device = "cuda"
            self.compute_type = (
                self.initial_compute_type
            )  # Usar el solicitado si es CUDA
        else:
            self.device = "cpu"
            self.compute_type = (
                "int8"  # 'int8' es común y eficiente para CPU en faster-whisper
            )

        # Asegúrate de que el directorio para los modelos exista
        self.model_data_dir.mkdir(parents=True, exist_ok=True)

        self.model = None  # Se carga bajo demanda
        logger.info(
            f"Transcriber inicializado. Modelo: {self.whisper_model_name}, Dispositivo: {self.device}, Tipo de cómputo: {self.compute_type}, Directorio de modelos: {self.model_data_dir}"
        )

    async def _load_model(self):
        """Carga el modelo Faster-Whisper si aún no está cargado."""
        if self.model is None:
            logger.info(
                f"Cargando modelo Faster-Whisper '{self.whisper_model_name}' en {self.device} con tipo de cómputo {self.compute_type}..."
            )
            # faster_whisper.WhisperModel.from_pretrained es síncrono, lo ejecutamos en un threadpool
            self.model = await asyncio.to_thread(
                WhisperModel,
                self.whisper_model_name,  # Usar el nombre del modelo
                device=self.device,
                compute_type=self.compute_type,
                download_root=str(
                    self.model_data_dir
                ),  # Especificar dónde faster_whisper debe buscar/descargar el modelo
            )
            logger.success("Modelo Faster-Whisper cargado exitosamente.")

    # ... (El resto de tus métodos transcribe, save_transcription_to_vtt, _format_timestamp son correctos) ...
    async def transcribe(self, audio_path: Path) -> TranscriptionResult:
        """
        Transcribe un archivo de audio usando Faster-Whisper y retorna un diccionario
        con la transcripción y los segmentos de texto con sus timestamps.
        """
        await self._load_model()
        logger.info(f"Transcribiendo audio: {audio_path}")
        try:
            segments_generator, info = await asyncio.to_thread(
                self.model.transcribe,
                str(audio_path),
                language="es",
                word_timestamps=False,
            )

            segments = []
            full_text = []
            for segment in segments_generator:
                segments.append(
                    {"start": segment.start, "end": segment.end, "text": segment.text}
                )
                full_text.append(segment.text)

            logger.success(f"Audio transcrito exitosamente: {audio_path}")
            return TranscriptionResult(
                text=" ".join(full_text),
                segments=segments,
                language=info.language,
            )
        except Exception as e:
            logger.error(
                f"Error al transcribir el audio {audio_path}: {e}", exc_info=True
            )
            raise

    async def save_transcription_to_vtt(
        self,
        output_path: Path,
        *,
        transcription_result: Optional[TranscriptionResult] = None,
        audio_path: Optional[Path] = None,
        clip_start_offset: float = 0.0,
        max_lines_per_cue: int = 2,
        max_chars_per_line: int = 32,
    ):
        """
        Guarda la transcripción en formato VTT, ajustando los timestamps con un offset.
        Puede recibir un `transcription_result` ya hecho o una ruta de `audio_path` para transcribir sobre la marcha.
        `clip_start_offset` debe estar en segundos.
        """
        if transcription_result is None and audio_path:
            logger.info(
                f"No se proporcionó transcripción, transcribiendo desde: {audio_path}"
            )
            transcription_result = await self.transcribe(audio_path)
        elif transcription_result is None:
            raise ValueError(
                "Se debe proporcionar 'transcription_result' o 'audio_path'."
            )
        logger.info(
            f"Guardando transcripción en VTT: {output_path} con offset {clip_start_offset:.2f}s"
        )
        try:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write("WEBVTT\n\n")

                idx = 1
                for segment in transcription_result.segments:
                    start = segment["start"] + clip_start_offset
                    end = segment["end"] + clip_start_offset
                    text = segment["text"].strip()

                    if not text:
                        continue

                    # Wrap del texto por palabras
                    lines = self._wrap_text(text, max_chars_per_line)
                    # Si supera el máximo de líneas, dividimos en sub-cues repartiendo el tiempo
                    sub_cues = self._split_lines_into_cues(lines, max_lines_per_cue)

                    # Repartir el tiempo del segmento en partes iguales por sub-cue
                    n = max(1, len(sub_cues))
                    seg_duration = max(0.8, end - start)
                    base = seg_duration / n
                    cur_start = start
                    for i, cue_lines in enumerate(sub_cues):
                        # Último cue ajusta exactamente al final para evitar acumulación de error
                        if i == n - 1:
                            cur_end = end
                        else:
                            cur_end = min(end, cur_start + base)
                        # Evitar cues de duración nula
                        if cur_end - cur_start <= 0.01:
                            continue
                        f.write(f"{idx}\n")
                        f.write(
                            f"{self._format_timestamp(cur_start)} --> {self._format_timestamp(cur_end)}\n"
                        )
                        f.write("\n".join(cue_lines) + "\n\n")
                        idx += 1
                        cur_start = cur_end
            logger.success(f"Transcripción VTT guardada exitosamente en: {output_path}")
        except Exception as e:
            logger.error(
                f"Error al guardar transcripción VTT en {output_path}: {e}",
                exc_info=True,
            )
            raise

    def _format_timestamp(self, seconds: float) -> str:
        """Formatea segundos a un string de timestamp VTT (HH:MM:SS.mmm)."""
        milliseconds = int((seconds - int(seconds)) * 1000)
        seconds = int(seconds)
        minutes = seconds // 60
        hours = minutes // 60
        seconds = seconds % 60
        minutes = minutes % 60
        return f"{hours:02}:{minutes:02}:{seconds:02}.{milliseconds:03}"

    def _wrap_text(self, text: str, max_chars_per_line: int) -> list[str]:
        words = text.split()
        lines: list[str] = []
        cur = []
        cur_len = 0
        for w in words:
            add_len = len(w) + (1 if cur else 0)
            if cur_len + add_len <= max_chars_per_line:
                cur.append(w)
                cur_len += add_len
            else:
                if cur:
                    lines.append(" ".join(cur))
                cur = [w]
                cur_len = len(w)
        if cur:
            lines.append(" ".join(cur))
        return lines

    def _split_lines_into_cues(
        self, lines: list[str], max_lines_per_cue: int
    ) -> list[list[str]]:
        cues: list[list[str]] = []
        for i in range(0, len(lines), max_lines_per_cue):
            cues.append(lines[i : i + max_lines_per_cue])
        return cues

    async def save_transcription_to_ass_karaoke(
        self,
        output_path: Path,
        *,
        keywords: set[str] | None = None,
        transcription_result: Optional[TranscriptionResult] = None,
        audio_path: Optional[Path] = None,
        clip_start_offset: float = 0.0,
        font_size: int = 48,
        margin_v: int = 120,
        color_base: str = "&H00FFFFFF",
        color_highlight: str = "&H0000FFFF",
    ) -> None:
        """
        Genera un archivo ASS con estilo karaoke por palabra. Usa \\k para avance cronológico
        de cada palabra y un override de color para keywords.
        Nota: requiere transcripción con word_timestamps.
        """
        if keywords is None:
            keywords = set()

        if transcription_result is None and audio_path:
            await self._load_model()
            logger.info(f"Transcribiendo audio con timestamps de palabra: {audio_path}")
            segments_generator, info = await asyncio.to_thread(
                self.model.transcribe,
                str(audio_path),
                language="es",
                word_timestamps=True,
            )
            segments = []
            full_text = []
            for seg in segments_generator:
                words = []
                if seg.words:
                    for w in seg.words:
                        words.append({"start": w.start, "end": w.end, "text": w.word})
                segments.append(
                    {
                        "start": seg.start,
                        "end": seg.end,
                        "text": seg.text,
                        "words": words,
                    }
                )
                full_text.append(seg.text)
            transcription_result = TranscriptionResult(
                text=" ".join(full_text), segments=segments, language=info.language
            )
        elif transcription_result is None:
            raise ValueError(
                "Se debe proporcionar 'transcription_result' o 'audio_path'."
            )

        # Cabecera ASS con estilo seguro (centrado abajo, contorno, sombra)
        header = (
            "[Script Info]\n"
            "ScriptType: v4.00+\n"
            "PlayResX: 1080\n"
            "PlayResY: 1920\n"
            "YCbCr Matrix: TV.709\n\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Default,Arial,{font_size},{color_base},{color_base},&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,4,0,2,30,30,{margin_v},1\n\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        )

        def ts_ass(seconds: float) -> str:
            ms = int((seconds - int(seconds)) * 100)
            seconds = int(seconds)
            m, s = divmod(seconds, 60)
            h, m = divmod(m, 60)
            return f"{h:01}:{m:02}:{s:02}.{ms:02}"

        def clean_word(w: str) -> str:
            return re.sub(r"\s+", " ", w.strip())

        def is_keyword(w: str) -> bool:
            return clean_word(w).lower() in keywords

        lines = [header]

        for seg in transcription_result.segments:
            start = seg.get("start", 0.0) + clip_start_offset
            end = seg.get("end", start + 1.0) + clip_start_offset
            words = seg.get("words") or []
            if not words:
                # Fallback: todo el texto como un solo bloque
                text = clean_word(seg.get("text", ""))
                if not text:
                    continue
                line = f"Dialogue: 0,{ts_ass(start)},{ts_ass(end)},Default,,0,0,0,,{text}\n"
                lines.append(line)
                continue

            # Construir karaoke con \k (centésimas de segundo)
            total_text = []
            for w in words:
                w_text = clean_word(w.get("text", ""))
                if not w_text:
                    continue
                w_dur = max(0.1, (w.get("end", start) - w.get("start", start)))
                k = int(w_dur * 100)  # centésimas
                if is_keyword(w_text):
                    # Color de palabra clave (bold por estilo global)
                    total_text.append(f"{{\\c{color_highlight}}}{{\\k{k}}}{w_text} ")
                else:
                    # Reset al color base
                    total_text.append(f"{{\\c{color_base}}}{{\\k{k}}}{w_text} ")

            ass_text = "".join(total_text).strip()
            if not ass_text:
                continue
            line = f"Dialogue: 0,{ts_ass(start)},{ts_ass(end)},Default,,0,0,0,,{ass_text}\n"
            lines.append(line)

        content = "".join(lines)
        try:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(content)
            logger.success(f"Transcripción ASS (karaoke) guardada en: {output_path}")
        except Exception as e:
            logger.error(f"Error al guardar ASS en {output_path}: {e}", exc_info=True)
            raise
