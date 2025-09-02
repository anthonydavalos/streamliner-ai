import asyncio
import datetime
from loguru import logger
from .config import AppConfig
from .storage.base import BaseStorage
from .pipeline import process_single_file # Importar el pipeline

class Downloader:
    """Gestiona la descarga de un stream usando streamlink."""

    def __init__(self, config: AppConfig, storage: BaseStorage):
        self.config = config
        self.storage = storage

    async def download_stream(self, streamer: str):
        """
        Invoca a streamlink como un subproceso no bloqueante para descargar un stream.
        Una vez finalizado, dispara el pipeline de procesamiento.
        """
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        output_filename = f"{streamer}_vod_{timestamp}.mp4"
        
        # La ruta de guardado depende del adaptador de almacenamiento
        local_filepath = await self.storage.get_local_path_for(output_filename)
        
        # Asegurarse de que el directorio padre exista
        local_filepath.parent.mkdir(parents=True, exist_ok=True)
        
        stream_url = f"https://kick.com/{streamer}"
        
        args = [
            "streamlink",
            stream_url,
            self.config.downloader.output_quality,
            "-o", str(local_filepath),
            "--force-progress" # Para ver el output
        ]

        logger.info(f"Ejecutando comando: {' '.join(args)}")
        
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        # Leer stdout y stderr de forma asíncrona para logging
        async def log_stream(stream, prefix):
            while True:
                line = await stream.readline()
                if line:
                    logger.debug(f"[{prefix}] {line.decode().strip()}")
                else:
                    break

        await asyncio.gather(
            log_stream(process.stdout, f"streamlink-{streamer}-out"),
            log_stream(process.stderr, f"streamlink-{streamer}-err")
        )
        
        return_code = await process.wait()

        if return_code == 0:
            logger.success(f"Descarga de VOD para {streamer} completada: {local_filepath}")
            
            # Si el almacenamiento no es local, subimos el archivo ahora.
            await self.storage.upload(local_filepath, output_filename)
            
            logger.info("🚀 Iniciando pipeline de procesamiento para el VOD recién descargado...")
            # dry_run=False porque esto es una ejecución de producción
            await process_single_file(self.config, str(local_filepath), streamer, dry_run=False)

        else:
            logger.error(f"Streamlink para {streamer} finalizó con código de error: {return_code}")