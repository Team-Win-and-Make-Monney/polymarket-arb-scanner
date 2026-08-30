FROM python:3.12-slim

WORKDIR /app

# ---------------------------------------------------------------------------
# System dependencies — libgomp1 is required by ONNX Runtime (fastembed).
# ---------------------------------------------------------------------------
RUN apt-get update && \
    apt-get install -y --no-install-recommends libgomp1 && \
    rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Python dependencies (cached unless requirements.txt changes).
# ---------------------------------------------------------------------------
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Run the networked scanner with a dedicated identity. Dependency installation
# stays privileged, while models, application files, and runtime state are
# owned only by the scanner user.
RUN groupadd --system scanner && \
    useradd --system --gid scanner --create-home --home-dir /home/scanner scanner && \
    mkdir -p /data && \
    chown -R scanner:scanner /data /home/scanner

ENV HOME=/home/scanner \
    DATA_DIR=/data

USER scanner

# ---------------------------------------------------------------------------
# Pre-download the fastembed model so it's baked into the image.
# Avoids a ~90 MB download on first scan (cold-start penalty in Fargate).
# ---------------------------------------------------------------------------
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('sentence-transformers/all-MiniLM-L6-v2')"

# ---------------------------------------------------------------------------
# Application code (changes most often — last layer for cache efficiency).
# ---------------------------------------------------------------------------
COPY --chown=scanner:scanner *.py ./
COPY --chown=scanner:scanner scans/ ./scans/
COPY --chown=scanner:scanner scripts/ ./scripts/

# Health check via unauthenticated healthz endpoint.
# Port resolution mirrors config.py: prefer DASHBOARD_PORT, fall back to Railway's
# injected PORT, then to 8080. The previous probe only read DASHBOARD_PORT and
# always fell through to 8080 on Railway (which sets PORT at runtime, not
# DASHBOARD_PORT) — fine when Railway picked 8080, but masked deploy failures
# whenever the injected port differed from the dashboard's actual bind port.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://localhost:{os.environ.get(\"DASHBOARD_PORT\") or os.environ.get(\"PORT\") or \"8080\"}/healthz')" || exit 1

# Railway's retained mission is narrow Kalshi research. The broad `all` mode
# duplicated venue fetches and produced multi-minute cycles that blinded the
# asyncio feed-health tasks. Broad scans remain available as an explicit CLI
# choice, but are not the unattended deployment default.
ENTRYPOINT ["python", "scanner.py", "--continuous", "--mode", "kalshi"]
