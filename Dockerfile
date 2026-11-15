FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends iputils-ping && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && \
    useradd --system --uid 10001 --create-home stempel && \
    mkdir -p /data && chown stempel:stempel /data && chmod 700 /data
COPY stempel ./stempel
USER stempel
ENV STEMPEL_DATA_DIR=/data TZ=Europe/Berlin
EXPOSE 8000
# Ein Worker, damit genau ein Hintergrund-Monitor läuft
CMD ["gunicorn", "--workers", "1", "--threads", "4", "--bind", "0.0.0.0:8000", "stempel.web:create_app()"]
