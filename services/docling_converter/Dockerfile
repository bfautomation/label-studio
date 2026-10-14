FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8090

WORKDIR /app

COPY pyproject.toml ./
COPY docling_converter_service ./docling_converter_service
COPY converter_api ./converter_api
COPY manage.py ./

RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      libgl1 \
      libglib2.0-0 \
      libsm6 \
      libxext6 \
      libxrender1 \
      libxcb1 && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

RUN useradd --create-home converter && chown -R converter:converter /app
USER converter

EXPOSE 8090

CMD ["gunicorn", "docling_converter_service.wsgi:application", "--bind", "0.0.0.0:8090", "--workers", "1", "--threads", "2", "--timeout", "3600"]
