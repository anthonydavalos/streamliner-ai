# src/streamliner/storage/__init__.py

from .base import BaseStorage
from .local import LocalStorage
from .s3 import S3Storage
from ..config import AppConfig


def get_storage(config: AppConfig) -> BaseStorage:
    """
    Factory function que devuelve una instancia del adaptador de almacenamiento
    configurado en el archivo .env.
    """
    storage_type = config.storage.storage_type

    if storage_type == "local":
        return LocalStorage(config)
    elif storage_type in ["s3", "r2"]:
        return S3Storage(config)
    else:
        raise ValueError(f"Tipo de almacenamiento desconocido: '{storage_type}'")
