#!/bin/bash
# Run this (or double-click, on file managers that support it) to start Live2Motion
# Photos in Docker on Linux. No editing of docker-compose.yml is needed - only the photos
# folder you choose is mounted, and everything (including which folder to
# convert) is configured from the web UI.
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required but wasn't found. Install it: https://docs.docker.com/engine/install/"
  read -p "Press Enter to close..."
  exit 1
fi

if [ ! -f config.json ]; then
  cp config.example.json config.json
fi

if [ ! -f .env ]; then
  # Mount only the photos folder (not the whole home folder), and run the
  # container as the current user so that folder stays writable.
  default_media="$HOME/Pictures"
  read -p "Photos folder to give Live2Motion access to [$default_media]: " media
  media="${media:-$default_media}"
  media="${media/#\~/$HOME}"
  if [ ! -d "$media" ]; then
    echo "Folder not found: $media"
    read -p "Press Enter to close..."
    exit 1
  fi
  {
    echo "HOST_MEDIA=$media"
    echo "LIVE2MOTION_UID=$(id -u)"
    echo "LIVE2MOTION_GID=$(id -g)"
  } > .env
fi

docker compose up -d --build

( sleep 3 && xdg-open "http://localhost:7000" 2>/dev/null ) &

echo
echo "Live2Motion is running in the background (Docker)."
echo "Your photos folder is mounted at /data - pick the exact photo folder under it from the in-app folder browser."
echo "To use a different photos folder, edit HOST_MEDIA in .env (not docker-compose.yml)."
echo "To stop it later: docker compose down"
read -p "Press Enter to close this window (the app keeps running in Docker)..."
