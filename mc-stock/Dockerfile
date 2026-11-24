FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    CONFIG_PATH=/data/config.yaml \
    DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

RUN groupadd --gid 1000 app && useradd --uid 1000 --gid app --no-create-home app \
    && mkdir -p /data && chown app:app /data
COPY checker.py .

USER app
ENTRYPOINT ["python", "/app/checker.py"]
