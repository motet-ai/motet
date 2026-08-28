FROM motet-worker:latest AS edge-worker

# ADR-0065 Docker image strategy:
# Keep local worker image aligned with cloud worker image and layer only host
# integration dependencies needed by local capabilities.
# ripgrep: preferred backend for core.file_grep on all edge workers.
#
# S3/SeaweedFS artifact store needs boto3 from the motet-worker image
# (docker/images/worker/requirements.txt, baked into the deps stage). Rebuild
# worker-deps / motet-worker when those requirements change or edge/builder
# workers will fail with "boto3 is required for …=s3".
#
# Builder-only dependencies (git, docker CLI/compose/buildx, safe.directory)
# live in docker/images/builder-worker/Dockerfile (ADR-0122), which layers on
# this image — keep this one device-safe.
RUN apt-get update && apt-get install -y --no-install-recommends \
    xdotool \
    xclip \
    ripgrep \
 && rm -rf /var/lib/apt/lists/*
