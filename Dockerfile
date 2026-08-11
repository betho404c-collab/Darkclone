FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    SCRIPT_PATH=/app/clonecat_forum_selecionar_topico.py

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY clonecat_agent.py .
COPY clonecat_forum_selecionar_topico.py .

RUN mkdir -p /data

CMD ["python", "-u", "clonecat_agent.py"]
