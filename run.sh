#!/usr/bin/env bash
# Запуск бота, веб-панели и планировщика.
set -euo pipefail
cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
exec .venv/bin/python -m app "$@"
