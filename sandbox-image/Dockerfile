FROM python:3.12-slim

RUN pip install --no-cache-dir \
    beancount==2.3.6 \
    pandas>=2.0 \
    matplotlib>=3.8 \
    && rm -rf /root/.cache

RUN useradd -m -s /bin/bash sandbox

RUN mkdir -p /data /workspace && chown sandbox:sandbox /data /workspace

USER sandbox
WORKDIR /workspace

CMD ["sleep", "infinity"]
