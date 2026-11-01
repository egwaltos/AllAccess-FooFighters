# Imagen base oficial de Python 3.12 ligera
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=America/Argentina/Buenos_Aires

WORKDIR /app

# Instalar certificados y zona horaria
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Instalar dependencias de Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el paquete del monitor
COPY allaccess_monitor/ ./allaccess_monitor/

# Crear directorio para estado persistente y usuario sin privilegios root
RUN mkdir -p /app/data && useradd -m appuser && chown -R appuser:appuser /app
USER appuser

ENV STATE_FILE_PATH=/app/data/.monitor_state.json

# Ejecutar el demonio de monitoreo continuo
CMD ["python", "-m", "allaccess_monitor"]
