#!/usr/bin/env bash
set -euo pipefail
if [[ $# -ne 1 || ! -f "$1" ]]; then echo "Usage: $0 /path/to/verified-contenthub.dump" >&2; exit 2; fi
echo "This replaces the Content Hub database only. Stop this project's containers first."
sha256sum --check "$1.sha256"
docker compose exec -T db pg_restore -U contenthub -d contenthub --clean --if-exists --no-owner < "$1"
