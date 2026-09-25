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

# ---------------------------------------------------------------------------
# Pre-download the fastembed model so it's baked into the image.
# Avoids a ~90 MB download on first scan (cold-start penalty in Fargate).
# ---------------------------------------------------------------------------
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('sentence-transformers/all-MiniLM-L6-v2')"

# ---------------------------------------------------------------------------
# Application code (changes most often — last layer for cache efficiency).
# ---------------------------------------------------------------------------
COPY *.py ./
COPY scans/ ./scans/
COPY scripts/ ./scripts/

# Create data directory for EFS mount (trades.db will live here)
RUN mkdir -p /data

# Health check via unauthenticated healthz endpoint.
# Port resolution mirrors config.py: prefer DASHBOARD_PORT, fall back to Railway's
# injected PORT, then to 8080. The previous probe only read DASHBOARD_PORT and
# always fell through to 8080 on Railway (which sets PORT at runtime, not
# DASHBOARD_PORT) — fine when Railway picked 8080, but masked deploy failures
# whenever the injected port differed from the dashboard's actual bind port.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://localhost:{os.environ.get(\"DASHBOARD_PORT\") or os.environ.get(\"PORT\") or \"8080\"}/healthz')" || exit 1

# Railway runs `research`: the Kalshi binary/multi scans plus the Layer 1
# structural strategies (Fréchet, temporal, CTF), observationally. It refuses to
# start unless DRY_RUN=true and never flips any strategy's execution flag. The
# broad `all` mode stays off: it duplicated venue fetches and produced
# multi-minute cycles that blinded the asyncio feed-health tasks.
# Note: SCAN_VENUES / PAPER_SCAN_VENUES must not be pinned to kalshi-only so
# research mode fetches Polymarket markets for Fréchet and CTF scans.
ENTRYPOINT ["python", "scanner.py", "--continuous", "--mode", "research"]
