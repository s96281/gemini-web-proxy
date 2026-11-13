# syntax=docker/dockerfile:1
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PLAYWRIGHT_BROWSERS_PATH=/opt/browsers \
    XDG_CONFIG_HOME=/app/state/config XDG_CACHE_HOME=/app/state/cache
WORKDIR /app
COPY pyproject.toml requirements.lock ./
COPY gemini_proxy ./gemini_proxy
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then \
      export PIP_CERT=/run/secrets/proxy_ca NODE_EXTRA_CA_CERTS=/run/secrets/proxy_ca; \
    fi; \
    apt-get update && apt-get install -y --no-install-recommends libatomic1 \
    && pip install -r requirements.lock && pip install --no-deps . \
    && playwright install --with-deps --no-shell chromium \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 proxyuser \
    && mkdir -p /app/state && chown -R proxyuser:proxyuser /app/state /opt/browsers
USER proxyuser
EXPOSE 8000
CMD ["gemini-proxy", "serve", "--host", "0.0.0.0", "--port", "8000"]
