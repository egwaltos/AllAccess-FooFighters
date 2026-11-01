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

# Puerto expuesto para Render / Railway / Fly.io y cron-job.org
EXPOSE 8000

# Healthcheck nativo del contenedor
HEALTHCHECK --interval=60s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; port = os.getenv('PORT', '8000'); urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=5)" || exit 1

# Ejecutar servidor HTTP ligero + demonio de monitoreo en segundo plano
CMD ["python", "-m", "allaccess_monitor.server"]
