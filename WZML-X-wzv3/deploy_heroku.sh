#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 --app <heroku-app-name> [--env-file .env.wzml] [--remote heroku-wzml]"
}

APP_NAME=""
ENV_FILE=".env.wzml"
REMOTE_NAME="heroku-wzml"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app)
      APP_NAME="${2:-}"
      shift 2
      ;;
    --env-file)
      ENV_FILE="${2:-}"
      shift 2
      ;;
    --remote)
      REMOTE_NAME="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1"
      usage
      exit 1
      ;;
  esac
done

if [[ -z "$APP_NAME" ]]; then
  echo "Error: --app is required."
  usage
  exit 1
fi

if ! command -v heroku >/dev/null 2>&1; then
  echo "Error: Heroku CLI is not installed."
  exit 1
fi

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Error: run this script from inside the WZML git repository."
  exit 1
fi

echo "Ensuring Heroku app exists: $APP_NAME"
if ! heroku apps:info -a "$APP_NAME" >/dev/null 2>&1; then
  heroku apps:create "$APP_NAME"
fi

echo "Setting buildpacks"
heroku buildpacks:clear -a "$APP_NAME"
heroku buildpacks:add heroku/python -a "$APP_NAME"

if [[ -f "$ENV_FILE" ]]; then
  echo "Applying env vars from $ENV_FILE"
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" ]] && continue
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    if [[ "$line" != *=* ]]; then
      continue
    fi
    key="${line%%=*}"
    value="${line#*=}"
    key="$(echo "$key" | xargs)"
    heroku config:set -a "$APP_NAME" "${key}=${value}" >/dev/null
  done < "$ENV_FILE"
else
  echo "Env file not found ($ENV_FILE). Skipping config:set."
fi

git remote get-url "$REMOTE_NAME" >/dev/null 2>&1 || \
  git remote add "$REMOTE_NAME" "https://git.heroku.com/${APP_NAME}.git"

echo "Pushing current branch to Heroku (main)"
git push "$REMOTE_NAME" HEAD:main

echo "Done. Check logs with:"
echo "  heroku logs --tail -a $APP_NAME"
