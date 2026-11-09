FROM python:3.12-slim

# ExifTool is required by both app.py (cleanup scan/preview, duration checks) and
# the vendored MotionPhoto2 muxer.
RUN apt-get update && \
    apt-get install -y --no-install-recommends libimage-exiftool-perl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Run as an unprivileged user. docker-compose passes the host UID/GID both as build
# args and as `user:`, so the files written to mounted folders belong to the host user
# and /app and the home dir are owned by that same user (no world-writable folders).
ARG UID=1000
ARG GID=1000
RUN groupadd -g ${GID} app && useradd -u ${UID} -g ${GID} -m -d /home/app app && \
    chown ${UID}:${GID} /app

COPY --chown=${UID}:${GID} . .

EXPOSE 7000

ENV LIVE2MOTION_DOCKER=1 HOME=/home/app
USER ${UID}:${GID}

CMD ["python", "app.py"]
