#!/bin/bash
# Deploy DVD Ripper to Remote Server

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load environment variables
if [[ -f "$SCRIPT_DIR/.env" ]]; then
    source "$SCRIPT_DIR/.env"
else
    echo "Error: .env file not found. Copy .env.example to .env and configure."
    exit 1
fi

# Validate required vars
: "${DEPLOY_HOST:?DEPLOY_HOST not set in .env}"
: "${DEPLOY_USER:?DEPLOY_USER not set in .env}"
: "${DEPLOY_PATH:?DEPLOY_PATH not set in .env}"

echo "Deploying to ${DEPLOY_USER}@${DEPLOY_HOST}:${DEPLOY_PATH}"

rsync -avz --delete \
    --exclude '.git' \
    --exclude '.env' \
    --exclude '*.swp' \
    --exclude '*~' \
    "$SCRIPT_DIR/" \
    "${DEPLOY_USER}@${DEPLOY_HOST}:${DEPLOY_PATH}/"

echo "Done."
