# MinIO's historical public image is no longer anonymously obtainable from Hub/Quay.
# Build the same release from official, immutable source instead of using a third-party mirror.
FROM golang:1.22.12-bookworm AS build
WORKDIR /src
ADD https://codeload.github.com/minio/minio/tar.gz/f92beb79b555ca0762e01d5aca11e531353e9eae /tmp/minio-source.tar.gz
RUN tar -xzf /tmp/minio-source.tar.gz --strip-components=1 -C /src
ENV CGO_ENABLED=0 GOMAXPROCS=4
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build go build -p 4 -trimpath -ldflags="-s -w" -o /minio .

FROM python:3.11.15-slim-bookworm
LABEL org.opencontainers.image.source="https://github.com/minio/minio" \
      org.opencontainers.image.revision="f92beb79b555ca0762e01d5aca11e531353e9eae" \
      org.opencontainers.image.licenses="AGPL-3.0-only"
COPY --from=build /minio /usr/local/bin/minio
COPY --from=build /src/LICENSE /usr/share/minio/LICENSE
COPY --from=build /tmp/minio-source.tar.gz /usr/share/minio/source.tar.gz
ENTRYPOINT ["minio"]
