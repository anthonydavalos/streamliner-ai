# src/streamliner/cutter.py

import asyncio
from pathlib import Path
from loguru import logger


class VideoCutter:
    """
    Clase responsable de cortar segmentos de video usando ffmpeg.
    """

    async def cut_clip(
        self,
        input_path: str | Path,
        output_path: str | Path,
        start_seconds: float,
        end_seconds: float,
    ) -> str | None:
        """
        Corta un clip desde 'input_path' usando los timestamps especificados.

        Args:
            input_path: Ruta al video de origen.
            output_path: Ruta donde se guardará el clip cortado.
            start_seconds: Tiempo de inicio del corte en segundos.
            end_seconds: Tiempo de finalización del corte en segundos.

        Returns:
            La ruta al archivo de salida si el corte fue exitoso, de lo contrario None.
        """
        logger.info(
            f"Cortando clip desde {start_seconds:.2f}s hasta {end_seconds:.2f}s..."
        )
        logger.info(f"Fuente: {input_path}")
        logger.info(f"Destino: {output_path}")

        # Asegurarse de que el directorio de salida exista
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        # Comando de ffmpeg para cortar el video.
        # -ss: Seek (buscar) hasta la posición de inicio. Ponerlo antes de -i es más rápido.
        # -to: Duración hasta la posición final.
        # -c copy: Copia los códecs de audio y video sin recodificar. Es ultra rápido.
        # -y: Sobrescribe el archivo de salida si ya existe.
        args = [
            "ffmpeg",
            "-y",
            "-ss",
            str(start_seconds),
            "-i",
            str(input_path),
            "-to",
            str(end_seconds),
            # Al no especificar "-c copy" ni "-map", ffmpeg recodificará
            # usando sus valores por defecto, creando un archivo 100% robusto.
            str(output_path),
        ]

        logger.debug(f"Ejecutando comando ffmpeg: {' '.join(args)}")

        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            error_message = stderr.decode().strip()
            logger.error(
                f"Error al cortar el video con ffmpeg (código: {process.returncode}):"
            )
            logger.error(error_message)
            return None

        logger.success(f"Clip cortado exitosamente y guardado en: {output_path}")
        return str(output_path)
