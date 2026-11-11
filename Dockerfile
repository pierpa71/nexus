FROM python:3.12-slim

# Imposta INSTALL_NODE=true per poter avviare server MCP stdio basati su Node (npx ...)
ARG INSTALL_NODE=false

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PORT=8088 \
    UV_CACHE_DIR=/data/uv-cache \
    UV_PYTHON_PREFERENCE=only-system \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && if [ "$INSTALL_NODE" = "true" ]; then apt-get install -y --no-install-recommends nodejs npm; fi \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# Verifica in fase di build che tutte le dipendenze siano presenti (evita container in crash loop)
RUN DATA_DIR=/tmp/buildcheck python -c "import app.main; print('Nexus', app.main.APP_VERSION, 'OK')" && rm -rf /tmp/buildcheck

RUN useradd -m -u 1000 nexus && mkdir -p /data && chown -R nexus /data
USER nexus
VOLUME ["/data"]
EXPOSE 8088

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD curl -fs http://localhost:${PORT}/api/health || exit 1

# Forma shell per espandere $PORT; exec mantiene uvicorn come PID 1 (gestione corretta dei segnali)
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers
