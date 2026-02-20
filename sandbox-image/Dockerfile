FROM python:3.12-slim

RUN pip install --no-cache-dir \
    beancount==2.3.6 \
    pandas>=2.0 \
    matplotlib>=3.8 \
    && rm -rf /root/.cache

# Install curl (needed for health-checking the browser server from the gateway)
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

# Copy the in-container browser automation server
COPY browser_server.py /opt/browser_server.py

RUN useradd -m -s /bin/bash sandbox

RUN mkdir -p /data /workspace && chown sandbox:sandbox /data /workspace

USER sandbox
WORKDIR /workspace

CMD ["sleep", "infinity"]
