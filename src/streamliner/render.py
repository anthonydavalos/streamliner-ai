# src/streamliner/render.py

import asyncio
import platform
import subprocess
from pathlib import Path
from loguru import logger
from typing import Optional


class VideoRenderer:
    def __init__(self, config):
        self.config = config
        logger.info(
            f"VideoRenderer inicializado con fg_zoom_factor: {self.config.fg_zoom_factor}, "
            f"fg_offset_x: '{self.config.fg_offset_x}', fg_offset_y: '{self.config.fg_offset_y}'"
        )

    async def _get_video_dimensions(self, video_path: Path) -> tuple[int, int]:
        """Obtiene el ancho y alto de un video usando ffprobe."""
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0:s=x",
            str(video_path),
        ]
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            logger.error(f"Error al obtener dimensiones del video: {stderr.decode()}")
            raise RuntimeError(
                f"Fallo al obtener dimensiones del video: {stderr.decode()}"
            )

        output = stdout.decode().strip()
        if "x" in output:
            width_str, height_str = output.split("x")
            return int(width_str), int(height_str)
        else:
            raise ValueError(
                f"No se pudieron parsear las dimensiones de ffprobe: {output}"
            )

    async def _probe_duration_seconds(self, video_path: Path) -> float:
        """Retorna la duración del video en segundos usando ffprobe o 0.0 en error."""
        try:
            cmd = [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nokey=1:noprint_wrappers=1",
                str(video_path),
            ]
            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            stdout, _ = await process.communicate()
            if process.returncode != 0:
                return 0.0
            try:
                return float(stdout.decode().strip())
            except Exception:
                return 0.0
        except Exception:
            return 0.0

    async def render_vertical_clip(
        self,
        input_path: Path,
        output_path: Path,
        srt_path: Optional[Path] = None,
    ) -> Path:
        """
        Crea un clip vertical 9:16 con fondo desenfocado y subtítulos quemados,
        o adapta un video ya vertical.
        """
        logger.info(f"Renderizando clip vertical: {output_path}")

        srt_filter_part = ""
        srt_final_path_for_ffmpeg_command: str = ""

        if srt_path and srt_path.exists():
            srt_posix_path = srt_path.resolve().as_posix()

            if (
                platform.system() == "Windows"
                and len(srt_posix_path) > 1
                and srt_posix_path[1] == ":"
            ):
                srt_final_path_for_ffmpeg_command = srt_posix_path.replace(
                    ":", "\\:", 1
                )
            else:
                srt_final_path_for_ffmpeg_command = srt_posix_path

            escaped_for_ffmpeg_quotes = srt_final_path_for_ffmpeg_command.replace(
                "'", "'\\''"
            )

            # Si el archivo es .ass, ya contiene estilos; no aplicar force_style
            if srt_path.suffix.lower() == ".ass":
                srt_filter_part = f"ass='{escaped_for_ffmpeg_quotes}'"
            else:
                # Es VTT/SRT, podemos forzar un estilo por defecto si no viene nada en config
                force_style = self.config.subtitle_style.strip()
                if not force_style:
                    # Estilo ASS por defecto seguro para 1080x1920
                    margin_v = getattr(self.config, "subtitle_safe_margin_v", 120)
                    font_size = getattr(self.config, "subtitle_font_size", 48)
                    # Notar: force_style espera claves ASS válidas
                    force_style = (
                        f"FontName=Arial,FontSize={font_size},Bold=1,PrimaryColour=&H00FFFFFF,"
                        f"OutlineColour=&H80000000,BackColour=&H80000000,BorderStyle=3,Outline=4,Shadow=0,"
                        f"Alignment=2,WrapStyle=2,MarginV={margin_v}"
                    )
                srt_filter_part = f"subtitles='{escaped_for_ffmpeg_quotes}':force_style='{force_style}'"
        else:
            logger.warning(
                "No se proporcionó archivo SRT para subtítulos o no existe. El clip se renderizará sin ellos."
            )

        output_width = 1080
        output_height = 1920

        input_width, input_height = await self._get_video_dimensions(input_path)
        logger.info(f"Dimensiones del video de entrada: {input_width}x{input_height}")

        # --- NUEVA LÓGICA DE RENDERIZADO ADAPTATIVO ---
        # Criterio para "Casi Vertical": si el ancho es al menos 80% del ancho de salida
        # Y la altura es al menos 80% de la altura de salida.
        is_almost_vertical = (
            input_width >= output_width * 0.8 and input_height >= output_height * 0.8
        )

        # Criterio para "Horizontal": si el aspect ratio es mayor que 1 (ancho > alto)
        is_horizontal = (input_width / input_height) > 1.0

        # Por defecto, asumimos que es un video horizontal que necesita el tratamiento clásico.
        # Si es casi vertical, cambiamos la estrategia.
        is_already_vertical_ish = is_almost_vertical and not is_horizontal

        filter_parts = []
        current_video_stream = ""
        logo_input_args = []

        # Factor de zoom para el primer plano (si aplica, en el modo de blur)
        # fg_zoom_factor = self.config.fg_zoom_factor
        # target_fg_height = int(output_height * fg_zoom_factor)

        if is_already_vertical_ish:
            logger.info(
                "El video de entrada es 'casi vertical'. Se escalará para llenar la altura y se rellenará con blur a los lados."
            )
            # 1. Dividir el stream
            filter_parts.append("[0:v]split=2[v_bg_src][v_fg_src];")
            # 2. Fondo: escalar para llenar 1080x1920, recortar y desenfocar
            filter_parts.append(
                f"[v_bg_src]scale={output_width}:{output_height}:force_original_aspect_ratio=increase,crop={output_width}:{output_height},boxblur=20:10[bg_filtered];"
            )
            # 3. Primer plano: escalar para que la altura sea 1920, manteniendo el aspect ratio
            filter_parts.append(f"[v_fg_src]scale=-1:{output_height}[fg_scaled];")
            # 4. Superponer el primer plano centrado sobre el fondo
            filter_parts.append(
                "[bg_filtered][fg_scaled]overlay=(W-w)/2:0[video_base];"
            )

        else:
            logger.info(
                "El video de entrada es horizontal. Se aplicará el renderizado con zoom y blur superior/inferior."
            )
            # Lógica original para videos horizontales

            # FILTRO PARA EL FONDO (BG):
            bg_filter = (
                f"scale={output_width}:{output_height}:force_original_aspect_ratio=increase,"
                f"crop={output_width}:{output_height},boxblur=20:10"
            )

            target_fg_height = int(output_height * self.config.fg_zoom_factor)
            # FILTRO PARA EL PRIMER PLANO (FG):
            fg_filter = (
                f"scale='iw*max({output_width}/iw,{target_fg_height}/ih):"
                f"ih*max({output_width}/iw,{target_fg_height}/ih)':force_original_aspect_ratio=disable,"
                f"crop={output_width}:{target_fg_height}"
            )

            # 1. Dividir el video original en dos streams: uno para el fondo, otro para el primer plano.
            filter_parts.append("[0:v]split=2[v_bg_src][v_fg_src];")

            # 2. Aplicar el filtro de fondo al stream de fondo.
            filter_parts.append(f"[v_bg_src]{bg_filter}[bg_filtered];")

            # 3. Aplicar el filtro de primer plano al stream de primer plano.
            filter_parts.append(f"[v_fg_src]{fg_filter}[fg_filtered];")

            # 4. Superponer el primer plano sobre el fondo. Resultado: [video_base]
            filter_parts.append(
                "[bg_filtered][fg_filtered]overlay=(W-w)/2:(H-h)/2[video_base];"
            )

        current_video_stream = "[video_base]"

        # 5. Añadir subtítulos si srt_filter_part existe.
        if srt_filter_part:
            filter_parts.append(
                f"{current_video_stream}{srt_filter_part}[video_after_subs];"
            )
            current_video_stream = "[video_after_subs]"

        # 6. Añadir el logo si self.config.logo_path existe.
        if self.config.logo_path and Path(self.config.logo_path).exists():
            logo_path_obj = Path(self.config.logo_path)
            logo_input_args = [
                "-i",
                logo_path_obj.resolve().as_posix(),
            ]

            filter_parts.append("[1:v]scale=150:-1[logo_scaled];")
            filter_parts.append(
                f"{current_video_stream}[logo_scaled]overlay=W-w-30:30[v_final];"
            )
            current_video_stream = "[v_final]"

        final_filter_complex = "".join(filter_parts)
        if final_filter_complex.endswith(";"):
            final_filter_complex = final_filter_complex[:-1]

        args = [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            *logo_input_args,
            "-filter_complex",
            final_filter_complex,
            "-map",
            current_video_stream,
            "-map",
            "0:a:0",
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
            str(output_path),
        ]

        logger.debug(f"Ejecutando ffmpeg para renderizar: {' '.join(args)}")

        process = await asyncio.create_subprocess_exec(
            *args, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )

        stdout, stderr = await process.communicate()

        # --- CORRECCIÓN PARA LIBERAR RECURSOS ---
        # Asegurarse de que todos los pipes se cierren explícitamente.
        # Esto es crucial en Windows para evitar que el proceso mantenga un bloqueo
        # sobre el archivo de salida, lo que causa errores de "Acceso Denegado".
        if process.stdin:
            await process.stdin.wait_closed()
        # -----------------------------------------

        if process.returncode != 0:
            err_text = stderr.decode()
            # Tolerancia: si el archivo de salida existe y tiene duración > 1s, consideramos éxito parcial.
            if output_path.exists():
                duration = await self._probe_duration_seconds(output_path)
                if duration >= 1.0:
                    logger.warning(
                        f"ffmpeg devolvió código {process.returncode}, pero el archivo resultante parece válido (duración ~{duration:.2f}s). Continuando."
                    )
                    return output_path
            logger.error(f"Error al renderizar el video: {err_text}")
            raise RuntimeError(f"Fallo en el renderizado con ffmpeg: {err_text}")
        else:
            logger.success(f"Clip vertical renderizado correctamente en {output_path}")
            return output_path
