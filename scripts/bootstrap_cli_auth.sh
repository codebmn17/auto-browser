#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

if [[ ! -t 0 || ! -t 1 ]]; then
  echo >&2 "bootstrap_cli_auth.sh must run in an interactive terminal"
  exit 1
fi

usage() {
  cat <<'USAGE'
Usage:
  ./scripts/bootstrap_cli_auth.sh [codex|claude|gemini|openai|all ...]

What it does:
  Opens the requested provider CLI interactively inside the controller image with
  HOME=/data/cli-home so the login/session state persists in ./data/cli-home.

Examples:
  ./scripts/bootstrap_cli_auth.sh codex
  ./scripts/bootstrap_cli_auth.sh claude gemini
  ./scripts/bootstrap_cli_auth.sh all
USAGE
}

if [[ $# -eq 0 ]]; then
  set -- all
fi

providers=()
for raw in "$@"; do
  case "${raw}" in
    openai|codex)
      providers+=(codex)
      ;;
    claude)
      providers+=(claude)
      ;;
    gemini)
      providers+=(gemini)
      ;;
    all)
      providers+=(codex claude gemini)
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo >&2 "Unknown provider: ${raw}"
      usage
      exit 1
      ;;
  esac
done

# De-duplicate while preserving order.
unique_providers=()
for provider in "${providers[@]}"; do
  skip=false
  for seen in "${unique_providers[@]:-}"; do
    if [[ "${seen}" == "${provider}" ]]; then
      skip=true
      break
    fi
  done
  if [[ "${skip}" == false ]]; then
    unique_providers+=("${provider}")
  fi
done

mkdir -p data/cli-home

for provider in "${unique_providers[@]}"; do
  echo
  echo "=== Bootstrapping ${provider} auth inside the controller container ==="
  echo "Complete login in the interactive CLI, then exit back to this shell."
  docker compose run --rm \
    --entrypoint bash \
    controller \
    -lc "export HOME=/data/cli-home; export NO_COLOR=1; exec ${provider}"
done

echo
echo "Done. Current auth cache contents:"
find data/cli-home -maxdepth 2 \( -name '.codex' -o -name '.claude' -o -name '.claude.json' -o -name '.gemini' \) -print | sed 's#^#- #' || true
