FROM python:3.12-slim

# ExifTool is required by both app.py (cleanup scan/preview, duration checks) and
# the vendored MotionPhoto2 muxer.
RUN apt-get update && \
    apt-get install -y --no-install-recommends libimage-exiftool-perl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 7000

ENV LIVE2MOTION_DOCKER=1

CMD ["python", "app.py"]
