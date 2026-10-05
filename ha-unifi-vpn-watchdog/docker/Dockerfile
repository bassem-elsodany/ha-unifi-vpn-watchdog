# Standalone image. Build from the project root:  docker build -f docker/Dockerfile -t ha-unifi-vpn-watchdog .
# Multi-arch for a Raspberry Pi:  docker buildx build --platform linux/arm64,linux/amd64 -f docker/Dockerfile -t ha-unifi-vpn-watchdog .
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
RUN useradd --system --uid 10001 --home /data watchdog && mkdir -p /data /config && chown watchdog /data
COPY --from=build /install /usr/local
USER watchdog
ENV PYTHONUNBUFFERED=1 WATCHDOG_CONFIG=/config/config.yaml
VOLUME ["/data", "/config"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=4).status==200 else 1)"
ENTRYPOINT ["ha-unifi-vpn-watchdog"]
CMD ["run"]
