# NOTE: written but not built in the authoring sandbox (no Docker daemon). CI builds it.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TRIAGE_HOST=0.0.0.0 \
    TRIAGE_PORT=8080 \
    TRIAGE_DB=/data/triage.db

WORKDIR /app
RUN useradd --system --uid 10001 --no-create-home triage \
 && mkdir /data && chown triage /data

COPY triage ./triage
COPY sample_data ./sample_data

USER triage
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status == 200 else 1)"

# Binding 0.0.0.0 requires TRIAGE_SERVER_TOKEN (the server refuses to start without one).
CMD ["python", "-m", "triage.server"]
