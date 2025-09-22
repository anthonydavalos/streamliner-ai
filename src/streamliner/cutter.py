# src/streamliner/cutter.py

import asyncio
import subprocess
from pathlib import Path
from loguru import logger


class VideoCutter:
    def __init__(self, clips_output_dir: Path):  # Aceptar AppConfig completa
        self.clips_dir = clips_output_dir  # <-- ¡CORREGIDO!
        self.clips_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            f"VideoCutter inicializado. Los clips se guardarán en: {self.clips_dir}"
        )

    async def cut_clip(
        self, input_path: Path, start_time: float, end_time: float, output_filename: str
    ) -> Path:
        """
        Corta un segmento de video usando ffmpeg.
        """
        output_path = self.clips_dir / output_filename
        duration = end_time - start_time

        logger.info(
            f"Cortando video desde {input_path} de {start_time:.2f} a {end_time:.2f} "
            f"({duration:.2f}s)"  # Dividido para cumplir con la longitud de línea (si aplica)
        )
        logger.debug(f"Guardando clip cortado en: {output_path}")

        command = [
            "ffmpeg",
            "-y",  # Sobrescribir archivos de salida sin pedir confirmación
            "-i",
            str(input_path),
            "-ss",
            str(start_time),
            "-t",
            str(duration),
            "-c:v",
            "copy",  # Copiar stream de video sin recodificar (más rápido)
            "-c:a",
            "copy",  # Copiar stream de audio sin recodificar (más rápido)
            str(output_path),
        ]

        process = await asyncio.create_subprocess_exec(
            *command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            logger.error(f"Error al cortar el video {input_path}: {stderr.decode()}")
            raise RuntimeError(f"FFmpeg cutting failed: {stderr.decode()}")
        else:
            logger.success(f"Video cortado exitosamente: {output_path}")
            return output_path
