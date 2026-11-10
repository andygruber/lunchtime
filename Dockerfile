# Multi-arch (linux/arm64 for OCI Ampere, linux/amd64): every dependency ships prebuilt wheels.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# OCR for menus published as images
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-deu \
 && rm -rf /var/lib/apt/lists/*

# dependencies first (cached unless pyproject.toml changes)
COPY pyproject.toml ./
RUN python -c "import tomllib; p = tomllib.load(open('pyproject.toml', 'rb'))['project']; \
print('\n'.join(p['dependencies'] + p['optional-dependencies']['llm']))" > /tmp/requirements.txt \
 && pip install -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt

COPY app ./app
COPY plugins ./plugins
# defaults; in production profiles/, rules/ and config/ are bind-mounted from the git checkout
COPY profiles ./profiles
COPY rules ./rules
COPY config ./config

ENV DATA_DIR=/data \
    PROFILES_DIR=/app/profiles \
    RULES_DIR=/app/rules \
    CONFIG_DIR=/app/config \
    TZ=Europe/Vienna

RUN mkdir -p /data && chmod 777 /data
USER 1000:1000
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"

# one worker: the import scheduler runs inside the app process
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
