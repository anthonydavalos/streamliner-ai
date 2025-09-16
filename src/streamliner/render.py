# src/streamliner/render.py - CÓDIGO CORREGIDO

import asyncio
import platform
from pathlib import Path
from loguru import logger


class VideoRenderer:
    def __init__(self, config):
        # self.config ahora es directamente el objeto RenderingConfig
        self.config = config

    async def render_vertical_clip(
        self, input_path: str, output_path: str, srt_path: str
    ):
        """
        Crea un clip vertical 9:16 con fondo desenfocado y subtítulos quemados.
        """
        logger.info(f"Renderizando clip vertical: {output_path}")

        srt_posix_path = Path(srt_path).resolve().as_posix()

        # Corrección del escape de ruta para Windows en FFmpeg:
        if platform.system() == "Windows":
            srt_final_path = srt_posix_path.replace(":", "\\:", 1)
        else:
            srt_final_path = srt_posix_path

        # El filtro de subtítulos requiere que la ruta esté entre comillas simples dentro del propio string del filtro.
        srt_final_path_in_filter = f"'{srt_final_path}'"

        output_width = 1080
        output_height = 1920  # Siempre será 1920 para el output final

        # --- Obtener fg_zoom_factor de la configuración ---
        # ¡CAMBIO AQUÍ! Acceder directamente como atributo del objeto self.config
        fg_zoom_factor = self.config.fg_zoom_factor

        # Calcular la altura deseada para el foreground, aplicando el zoom factor
        target_fg_height = int(output_height * fg_zoom_factor)

        # FILTRO PARA EL FONDO (BG):
        # Escala el video para que llene completamente el 1080x1920
        # (se hará un "zoom" si es 16:9, recortando los lados) y luego lo desenfoca.
        bg_filter = (
            f"scale={output_width}:{output_height}:force_original_aspect_ratio=increase,"
            f"crop={output_width}:{output_height},boxblur=20:10"
        )

        # FILTRO PARA EL PRIMER PLANO (FG):
        fg_filter = (
            f"scale='h={target_fg_height}:w=-2':force_original_aspect_ratio=increase,"
            f"crop={output_width}:{target_fg_height}"  # El crop aquí asegura que el ancho máximo del FG sea 1080 y la altura sea target_fg_height.
        )

        # --- LÓGICA PARA FG_OFFSET_X y FG_OFFSET_Y ---
        # ¡CAMBIO AQUÍ! Acceder directamente como atributos del objeto self.config
        fg_offset_x_cfg = self.config.fg_offset_x
        fg_offset_y_cfg = self.config.fg_offset_y

        # Convertir 'center' o valores numéricos a expresiones de FFmpeg
        # (W-w)/2 centra el primer plano (w) en el fondo (W)
        overlay_x = "(W-w)/2" if fg_offset_x_cfg == "center" else str(fg_offset_x_cfg)
        overlay_y = "(H-h)/2" if fg_offset_y_cfg == "center" else str(fg_offset_y_cfg)
        # --------------------------------------------------

        logo_input = []
        final_filter_complex = ""

        # ¡CAMBIO AQUÍ! Acceder directamente como atributo del objeto self.config
        if self.config.logo_path and Path(self.config.logo_path).exists():
            logo_posix_path = Path(self.config.logo_path).resolve().as_posix()
            logo_input = ["-i", logo_posix_path]

            final_filter_complex = f"""
[0:v]split=2[v1][v2];
[v1]{bg_filter}[bg];
[v2]{fg_filter}[fg];
[bg][fg]overlay={overlay_x}:{overlay_y},
subtitles={srt_final_path_in_filter}:force_style='{self.config.subtitle_style}'[video_with_subs];
[1:v]scale=150:-1[logo];
[video_with_subs][logo]overlay=W-w-30:30
"""
        else:
            final_filter_complex = f"""
[0:v]split=2[v1][v2];
[v1]{bg_filter}[bg];
[v2]{fg_filter}[fg];
[bg][fg]overlay={overlay_x}:{overlay_y},
subtitles={srt_final_path_in_filter}:force_style='{self.config.subtitle_style}'
"""
        final_filter_complex = final_filter_complex.strip()

        args = [
            "ffmpeg",
            "-y",
            "-i",
            input_path,
            *logo_input,
            "-filter_complex",
            final_filter_complex,
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "22",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            output_path,
        ]

        logger.debug(f"Ejecutando ffmpeg para renderizar: {' '.join(args)}")

        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            logger.error(f"Error al renderizar el video: {stderr.decode()}")
            raise RuntimeError("Fallo en el renderizado con ffmpeg.")
        else:
            logger.success(f"Clip vertical renderizado correctamente en {output_path}")
            return output_path
