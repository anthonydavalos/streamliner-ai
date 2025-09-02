# Dentro de src/streamliner/render.py
# Reemplaza la función render_vertical_clip completa con esta versión final.

import asyncio
import platform
from pathlib import Path
from loguru import logger

class VideoRenderer:
    def __init__(self, config):
        self.config = config.rendering

    async def render_vertical_clip(self, input_path: str, output_path: str, srt_path: str):
        """
        Crea un clip vertical 9:16 con fondo desenfocado y subtítulos quemados.
        """
        logger.info(f"Renderizando clip vertical: {output_path}")

        # --- INICIO DE LA CORRECCIÓN FINAL ---
        srt_posix_path = Path(srt_path).resolve().as_posix()
        
        # Para FFmpeg en Windows, la ruta del filtro de subtítulos debe ser formateada
        # de una manera muy específica para evitar errores de parseo.
        if platform.system() == "Windows":
            # 1. Escapamos la barra invertida para el carácter de escape
            # 2. Escapamos los dos puntos del drive letter (C:)
            # 3. Envolvemos toda la ruta en comillas simples
            srt_escaped_path = srt_posix_path.replace('\\', '/').replace(':', '\\:')
            srt_final_path = f"'{srt_escaped_path}'"
        else:
            srt_final_path = srt_posix_path
        # --- FIN DE LA CORRECCIÓN FINAL ---

        filter_complex = (
            # Escala el video principal para que ocupe todo el ancho 1080px (o el alto si es vertical)
            # Luego lo centra y lo usa como fondo con desenfoque.
            f"[0:v]split=2[v1][v2];"
            f"[v1]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
            f"boxblur=20:10[bg];" # Fondo desenfocado

            # Escala la parte principal del video. Si el original es horizontal, lo hará más pequeño
            # para que quepa en el centro del clip vertical (por ejemplo, 1080x608 para 16:9 en 9:16).
            f"[v2]scale='w=min(iw,1080)':'h=-2':force_original_aspect_ratio=decrease[fg];" # Primer plano

            # Superpone el video principal (fg) sobre el fondo (bg) y lo centra.
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,"

            # Añade los subtítulos
            f"subtitles={srt_final_path}:force_style='{self.config.subtitle_style}'"
        )
        
        logo_input = []
        if self.config.logo_path and Path(self.config.logo_path).exists():
            logo_posix_path = Path(self.config.logo_path).resolve().as_posix()
            logo_input = ["-i", logo_posix_path]
            filter_complex += "[out];[1:v]scale=150:-1[logo];[out][logo]overlay=W-w-30:30"
            
        args = [
            "ffmpeg", "-y", "-i", input_path,
            *logo_input,
            "-filter_complex", filter_complex,
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-c:a", "aac", "-b:a", "192k",
            output_path
        ]
        
        logger.debug(f"Ejecutando ffmpeg para renderizar: {' '.join(args)}")
        
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            logger.error(f"Error al renderizar el video: {stderr.decode()}")
            raise RuntimeError("Fallo en el renderizado con ffmpeg.")
        else:
            logger.success(f"Clip vertical renderizado correctamente en {output_path}")
            return output_path