# src/streamliner/storage/base.py

from abc import ABC, abstractmethod
from pathlib import Path


class BaseStorage(ABC):
    """
    Clase base abstracta para los adaptadores de almacenamiento.
    Define la interfaz común que todos los sistemas de almacenamiento deben implementar.
    """

    @abstractmethod
    async def upload(self, local_path: Path, remote_filename: str) -> str:
        """Sube un archivo local a un almacenamiento remoto."""
        pass

    @abstractmethod
    async def download(self, remote_filename: str, local_path: Path) -> bool:
        """Descarga un archivo remoto a una ubicación local."""
        pass

    @abstractmethod
    async def get_public_url(self, remote_filename: str) -> str | None:
        """Obtiene una URL de acceso público para un archivo."""
        pass

    @abstractmethod
    async def get_local_path_for(self, filename: str) -> Path:
        """
        Devuelve la ruta local donde un archivo debe ser guardado temporalmente o permanentemente.
        """
        pass
