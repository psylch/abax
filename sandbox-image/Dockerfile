FROM python:3.12-slim

RUN pip install --no-cache-dir \
    beancount==2.3.6 \
    pandas>=2.0 \
    matplotlib>=3.8 \
    && rm -rf /root/.cache

# Install curl (needed for health-checking the daemon from the gateway)
# and Playwright with its bundled Chromium + system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
RUN pip install --no-cache-dir \
    playwright \
    uvicorn \
    fastapi \
    && playwright install chromium --with-deps \
    && chmod -R a+rx /opt/pw-browsers \
    && rm -rf /root/.cache

# Copy the universal sandbox daemon
COPY sandbox_server.py /opt/sandbox_server.py

RUN useradd -m -s /bin/bash sandbox

RUN mkdir -p /data /workspace && chown sandbox:sandbox /data /workspace

USER sandbox
WORKDIR /workspace

# Start the sandbox daemon on port 8331
CMD ["python3", "/opt/sandbox_server.py"]
