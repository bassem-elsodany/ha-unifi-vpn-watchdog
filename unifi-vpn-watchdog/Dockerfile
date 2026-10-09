# Home Assistant add-on image (HA builds this from the add-on folder). For plain Docker use docker/Dockerfile.
ARG BUILD_FROM=python:3.12-slim
FROM ${BUILD_FROM}
RUN (command -v apk >/dev/null && apk add --no-cache python3 py3-pip || true)
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --break-system-packages . 2>/dev/null || pip install --no-cache-dir .
ENV WATCHDOG_CONFIG=/config/config.yaml PYTHONUNBUFFERED=1
CMD ["python", "-m", "vpn_watchdog.ha_entry"]
