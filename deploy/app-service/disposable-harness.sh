# Sourced by scripts that must run the harness on one given image and leave nothing behind.

# Start a harness from empty volumes on exactly IMAGE, without building or pulling it, and remove it with its volumes
# when the calling script exits, whether its checks pass or fail. Runs as a Compose project and on a port of its own,
# so an interactive harness on this machine is left untouched. Call it from deploy/app-service.
# Usage: start_disposable_harness IMAGE NAME
start_disposable_harness() {
  export HARNESS_IMAGE=$1
  docker image inspect "$HARNESS_IMAGE" >/dev/null 2>&1 || { echo "Image not found locally: $HARNESS_IMAGE" >&2; exit 1; }

  ./setup.sh >/dev/null
  export COMPOSE_PROJECT_NAME=label-studio-$2-$$
  # the public address includes the port, so it is chosen before start-up
  HARNESS_HTTPS_PORT=$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
  export HARNESS_HTTPS_PORT
  trap remove_disposable_harness EXIT
  # every service except app and blob-init, which run the image given
  docker compose pull --quiet frontdoor db blob home-init
  # never build or pull the image given: the checks must run against it, and only it
  docker compose up -d --no-build --pull never
}

remove_disposable_harness() {
  docker compose down -v --remove-orphans >/dev/null 2>&1
}
