FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE /app/
COPY requirements /app/requirements
COPY mealcircuit /app/mealcircuit
COPY sync_server /app/sync_server
COPY protocol /app/protocol
RUN pip install --no-cache-dir -r requirements/sync-server.lock \
    && pip install --no-cache-dir --no-deps .

RUN useradd --create-home --uid 10001 mealcircuit \
    && mkdir -p /var/lib/mealcircuit-sync/blobs \
    && chown -R mealcircuit:mealcircuit /var/lib/mealcircuit-sync
USER mealcircuit

EXPOSE 8080
CMD ["sh", "-c", "alembic -c sync_server/alembic.ini upgrade head && exec uvicorn sync_server.main:app --host 0.0.0.0 --port 8080 --proxy-headers"]
