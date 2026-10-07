# syntax=docker/dockerfile:1
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/home/appuser/.local/bin:$PATH"

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libgomp1 && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

# Install CPU-only PyTorch and the application dependencies in one uv
# resolve. unsafe-best-match lets torch/torchvision pick the "+cpu" builds
# from the PyTorch index (they sort above the CUDA builds on PyPI), so no
# nvidia-* packages are pulled in. The uv cache lives in a BuildKit cache
# mount: reused across builds, never part of the image.
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_LINK_MODE=copy uv pip install --system \
    -r requirements.txt torch torchvision \
    --extra-index-url https://download.pytorch.org/whl/cpu \
    --index-strategy unsafe-best-match

COPY app ./app

RUN useradd --create-home --shell /bin/bash appuser && \
    chown -R appuser:appuser /app

USER appuser

EXPOSE 8080

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]