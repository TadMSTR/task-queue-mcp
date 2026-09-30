FROM python:3.12-slim

# Set by the publish workflow to the release tag and commit. The defaults mark a local
# build, so `docker inspect` never reports a version an image was not cut from (vikunja#362).
ARG VERSION=dev
ARG REVISION=unknown

LABEL org.opencontainers.image.source="https://github.com/TadMSTR/task-queue-mcp" \
      org.opencontainers.image.title="task-queue-mcp" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

WORKDIR /app

# Create non-root user matching host UID 1000 (ted)
RUN groupadd -g 1000 ted && useradd -u 1000 -g 1000 -s /sbin/nologin -M ted

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

EXPOSE 8485

USER 1000

CMD ["python", "-m", "src.server"]
