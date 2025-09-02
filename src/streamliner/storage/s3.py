# src/streamliner/storage/s3.py

from pathlib import Path
from loguru import logger
from aiobotocore.session import get_session
from .base import BaseStorage
from ..config import AppConfig


class S3Storage(BaseStorage):
    """
    Implementación del almacenamiento para AWS S3 y compatibles (Cloudflare R2).
    """

    def __init__(self, config: AppConfig):
        self.config = config.storage
        self.local_temp_dir = Path(config.downloader.local_storage_path) / "temp"
        self.local_temp_dir.mkdir(parents=True, exist_ok=True)

        session = get_session()
        self.client_creator = session.create_client(
            "s3",
            region_name=self.config.aws_s3_region,
            endpoint_url=self.config.aws_s3_endpoint_url,  # Esencial para R2
            aws_access_key_id=self.config.aws_access_key_id,
            aws_secret_access_key=self.config.aws_secret_access_key,
        )
        logger.info(
            f"Usando almacenamiento S3/R2. Bucket: {self.config.aws_s3_bucket_name}"
        )

    async def upload(self, local_path: Path, remote_filename: str) -> str:
        """Sube un archivo local al bucket de S3/R2."""
        logger.info(
            f"Subiendo {local_path} a S3 bucket {self.config.aws_s3_bucket_name} como {remote_filename}..."
        )
        try:
            async with self.client_creator as client:
                with open(local_path, "rb") as f:
                    await client.put_object(
                        Bucket=self.config.aws_s3_bucket_name,
                        Key=remote_filename,
                        Body=f,
                    )
            logger.success(f"Archivo subido exitosamente a S3: {remote_filename}")
            return remote_filename
        except Exception as e:
            logger.error(f"Fallo al subir a S3: {e}")
            raise

    async def download(self, remote_filename: str, local_path: Path) -> bool:
        """Descarga un archivo desde S3/R2 a una ruta local."""
        logger.info(f"Descargando {remote_filename} de S3 a {local_path}...")
        try:
            async with self.client_creator as client:
                response = await client.get_object(
                    Bucket=self.config.aws_s3_bucket_name, Key=remote_filename
                )
                async with response["Body"] as stream:
                    with open(local_path, "wb") as f:
                        f.write(await stream.read())
            logger.success("Descarga de S3 completada.")
            return True
        except Exception as e:
            logger.error(f"Fallo al descargar de S3: {e}")
            return False

    async def get_public_url(self, remote_filename: str) -> str | None:
        """Genera una URL pública para un objeto en S3/R2."""
        # Nota: Esto asume que el bucket está configurado para acceso público
        # o que tienes una CDN como CloudFront/Cloudflare en frente.
        if self.config.aws_s3_endpoint_url:  # Cloudflare R2
            # Las URLs de R2 públicas deben ser construidas manualmente si tienes un dominio público
            # Aquí asumimos una URL base, esto debe ser configurado por el usuario.
            # Por ejemplo: "https://pub-<ID>.r2.dev/<BUCKET_NAME>/<FILENAME>"
            logger.warning(
                "La generación de URL pública para R2 requiere configuración manual del dominio."
            )
            return None

        # Para AWS S3
        return f"https://{self.config.aws_s3_bucket_name}.s3.{self.config.aws_s3_region}.amazonaws.com/{remote_filename}"

    async def get_local_path_for(self, filename: str) -> Path:
        """Para S3, los archivos se descargan a un directorio temporal."""
        return self.local_temp_dir / filename
