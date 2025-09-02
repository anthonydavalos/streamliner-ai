# src/streamliner/storage/local.py

import shutil
from pathlib import Path
from loguru import logger
from .base import BaseStorage
from ..config import AppConfig


class LocalStorage(BaseStorage):
    """
    Implementación del almacenamiento para guardar archivos en el disco local.
    """

    def __init__(self, config: AppConfig):
        self.base_path = Path(config.downloader.local_storage_path)
        self.base_path.mkdir(parents=True, exist_ok=True)
        logger.info(f"Usando almacenamiento local en: {self.base_path.resolve()}")

    async def upload(self, local_path: Path, remote_filename: str) -> str:
        """
        Para el almacenamiento local, "subir" significa mover el archivo a la
        estructura de carpetas final, si es necesario. En este caso, el archivo ya está
        en su lugar final, así que solo confirmamos la ruta.
        """
        final_path = self.base_path / remote_filename
        if local_path != final_path:
            shutil.move(str(local_path), str(final_path))
        logger.info(f"Archivo confirmado en almacenamiento local: {final_path}")
        return str(final_path)

    async def download(self, remote_filename: str, local_path: Path) -> bool:
        """Para el almacenamiento local, "descargar" es simplemente una copia de archivo."""
        source_path = self.base_path / remote_filename
        if not source_path.exists():
            logger.error(
                f"El archivo no existe en el almacenamiento local: {source_path}"
            )
            return False
        shutil.copy(str(source_path), str(local_path))
        return True

    async def get_public_url(self, remote_filename: str) -> str | None:
        """El almacenamiento local no proporciona URLs públicas."""
        logger.warning("get_public_url no está soportado para el almacenamiento local.")
        return None

    async def get_local_path_for(self, filename: str) -> Path:
        """Devuelve la ruta completa donde se debe guardar un archivo."""
        return self.base_path / filename
