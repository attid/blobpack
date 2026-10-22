# blobpack — collect files into one SQLite database of blobs.
FROM python:3.12-alpine

ARG VERSION=dev
ARG REVISION=unknown

LABEL org.opencontainers.image.title="blobpack" \
      org.opencontainers.image.description="Collect files into one SQLite database of blobs so a database backup tool covers them" \
      org.opencontainers.image.source="https://github.com/attid/blobpack" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

ENV PYTHONUNBUFFERED=1 \
    BACKUP_VERSION="${VERSION}" \
    BACKUP_REVISION="${REVISION}"

WORKDIR /opt/blobpack
COPY bin/collector.py bin/healthcheck.sh /opt/blobpack/
RUN chmod 0755 /opt/blobpack/healthcheck.sh \
    && python3 -m py_compile /opt/blobpack/collector.py

# Shell-only probe: sqlite file present, and the last successful collection is
# younger than BACKUP_MAX_AGE. Swarm inherits this when the service does not set
# its own healthcheck.
HEALTHCHECK --interval=1h --timeout=10s --start-period=2m --retries=1 \
    CMD ["/bin/sh", "/opt/blobpack/healthcheck.sh"]

ENTRYPOINT ["python3", "/opt/blobpack/collector.py"]
CMD ["run"]
