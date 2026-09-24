# ICRISAT Data Hub — upload web app (deployed mode)
# Hosts: Railway / Render / Fly.io (Dockerfile detected automatically).
# Mount a persistent volume at /data and set env vars (see README "Deploy online").
FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONPATH=src \
    HUB_DATA_DIR=/data

# The host injects PORT (hub.web.app reads it); 8010 is the local default.
EXPOSE 8010

CMD ["python", "-m", "hub.web.app"]
