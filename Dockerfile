# Usa una imagen base de Python delgada
FROM python:3.11-slim

# Establece el directorio de trabajo
WORKDIR /app

# Instala dependencias del sistema: ffmpeg y git (para algunas dependencias de Python)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copia el archivo de requerimientos primero para aprovechar el cache de Docker
COPY requirements.txt .

# Instala las dependencias de Python
RUN pip install --no-cache-dir -r requirements.txt

# Copia el resto del código de la aplicación
COPY ./src /app/src

# Expone el puerto si fuera necesario (no en este caso para un CLI)
# EXPOSE 8000

# Comando por defecto para ejecutar la aplicación
# Se puede sobreescribir con docker-compose o en la línea de comandos
ENTRYPOINT ["python", "-m", "src.streamliner.cli"]
CMD ["monitor"]