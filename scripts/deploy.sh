#!/usr/bin/env sh
# Deploy or update the production stack on the server: pulls the checked-out branch
# (normally main) and rebuilds/restarts the containers. Migrations and the first
# airport import run automatically in the `migrate` service.
set -eu
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "Missing: .env (template: .env.example)" >&2
  exit 1
fi

git pull --ff-only
docker compose up -d --build --remove-orphans
docker image prune -f >/dev/null
docker compose ps
