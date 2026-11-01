FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend/src \
    PIP_DEFAULT_TIMEOUT=300 \
    PIP_RETRIES=5 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MEDIA_ROOT=/app/data/media \
    DATABASE_PATH=/app/data/video_intelligence.sqlite3 \
    AUTH_DATABASE_PATH=/app/data/auth.sqlite3 \
    AUDIT_DATABASE_PATH=/app/data/auth.sqlite3

WORKDIR /app
ARG INSTALL_ML=0
ARG TORCH_VARIANT=cpu

RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir --prefer-binary --timeout 300 --retries 5 -r requirements.lock

COPY backend/requirements-ml.lock ./requirements-ml.lock
COPY backend/requirements-ml.cpu.lock ./requirements-ml.cpu.lock
COPY backend/requirements-ml.cuda.lock ./requirements-ml.cuda.lock

RUN if [ "$INSTALL_ML" = "1" ] || [ "$INSTALL_ML" = "true" ]; then \
      if [ "$TORCH_VARIANT" = "cuda" ]; then \
        pip install --no-cache-dir --prefer-binary --timeout 300 --retries 5 \
          --index-url https://download.pytorch.org/whl/cu124 \
          --extra-index-url https://pypi.org/simple \
          -r requirements-ml.cuda.lock; \
      else \
        pip install --no-cache-dir --prefer-binary --timeout 300 --retries 5 \
          --index-url https://download.pytorch.org/whl/cpu \
          --extra-index-url https://pypi.org/simple \
          -r requirements-ml.cpu.lock; \
      fi; \
      pip install --no-cache-dir --prefer-binary --timeout 300 --retries 5 -r requirements-ml.lock; \
      pip uninstall -y opencv-python opencv-contrib-python opencv-contrib-python-headless >/dev/null 2>&1 || true; \
      pip install --no-cache-dir --prefer-binary --no-deps --force-reinstall \
        --timeout 300 --retries 5 opencv-python-headless==4.10.0.84; \
    fi

COPY backend/pyproject.toml ./pyproject.toml
COPY README.md ./README.md
COPY backend/src ./backend/src
COPY backend/tests ./backend/tests
COPY frontend ./frontend

# Create storage hierarchy for actual video files, evidence clips, and databases
RUN mkdir -p /app/data/media /app/data/media/evidence

# Explicit Docker volume declaration for persistent storage of actual video files
VOLUME ["/app/data"]

EXPOSE 8000
HEALTHCHECK --interval=20s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health/ready', timeout=3)" || exit 1

CMD ["python", "-m", "uvicorn", "video_intelligence.api:app", "--host", "0.0.0.0", "--port", "8000"]
