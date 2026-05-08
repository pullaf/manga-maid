FROM python:3.12-alpine

ARG MDX_VERSION=1.16.1
ARG SUPERCRONIC_VERSION=0.2.45
ARG TARGETARCH

RUN ARCH=$([ "$TARGETARCH" = "arm64" ] && echo "arm64" || echo "amd64") \
    && wget -qO- https://github.com/arimatakao/mdx/releases/download/v${MDX_VERSION}/mdx_v${MDX_VERSION}_linux_${ARCH}.tar.gz \
    | tar xz -C /usr/local/bin mdx

RUN ARCH=$([ "$TARGETARCH" = "arm64" ] && echo "arm64" || echo "amd64") \
    && wget -qO /usr/local/bin/supercronic \
       https://github.com/aptible/supercronic/releases/download/v${SUPERCRONIC_VERSION}/supercronic-linux-${ARCH} \
    && chmod +x /usr/local/bin/supercronic

RUN apk add --no-cache su-exec tzdata && \
    pip install --no-cache-dir fastapi uvicorn jinja2 python-multipart

COPY entrypoint.sh /entrypoint.sh
COPY manga-fix.py manga-sync.py manga-fix.sh sync_config.py kavita.py db.py comicinfo.py comicinfo_defs.py cron_enqueue_sync.py file_permissions.py /app/
COPY web/ /app/web/

RUN chmod +x /entrypoint.sh && mkdir -p /data/config /data/logs

EXPOSE 4649

ENTRYPOINT ["/entrypoint.sh"]
