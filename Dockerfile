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

COPY entrypoint.sh /entrypoint.sh
COPY manga-fix.py manga-sync.py /app/

RUN chmod +x /entrypoint.sh && mkdir -p /logs

ENTRYPOINT ["/entrypoint.sh"]
