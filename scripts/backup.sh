#!/usr/bin/env bash
set -euo pipefail
if [[ $# -ne 1 ]]; then echo "Usage: $0 /absolute/path/on-backup-volume/contenthub.dump" >&2; exit 2; fi
dest="$1"
mkdir -p "$(dirname "$dest")"
umask 077
docker compose exec -T db pg_dump -U contenthub -d contenthub --format=custom > "$dest"
docker compose exec -T db pg_restore --list < "$dest" >/dev/null
sha256sum "$dest" > "$dest.sha256"
echo "Backup verified: $dest"
