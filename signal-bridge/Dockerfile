FROM python:3.12-slim

RUN useradd --create-home --uid 1000 bridge

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY --chmod=755 entrypoint.sh /entrypoint.sh

RUN mkdir -p /logs /config && chown -R bridge:bridge /app /logs /config

EXPOSE 5000 8081 5556

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/health', timeout=3).status == 200 else 1)"

# Starts as root only to fix /logs ownership, then drops to "bridge".
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "app.server"]
