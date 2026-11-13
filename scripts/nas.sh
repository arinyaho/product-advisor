#!/usr/bin/env bash
# Deploy and operate the book MCP server on a Docker host (a Synology NAS) from any machine that can reach it.
#
#   scripts/nas.sh HOST deploy BOOK.pdf         copy the server and the book, build, start, wait until ready
#   scripts/nas.sh HOST issue-key NAME --days N [--scope full|excerpt]
#   scripts/nas.sh HOST renew-key NAME --days N
#   scripts/nas.sh HOST revoke-key NAME
#   scripts/nas.sh HOST list-keys
#   scripts/nas.sh HOST logs | status | down
#
# HOST is an ssh destination such as admin@nas.local, or "local" to run on this machine.
# Environment:
#   NAS_DIR             project directory on the host (default /volume1/docker/product-advisor)
#   DOCKER              docker command on the host (default "sudo /usr/local/bin/docker" over ssh, where DSM
#                       keeps it outside the ssh and sudo PATH; "docker" for local)
#   PORT                host port the container listens on, bound to 127.0.0.1 (default 8765)
#   OPENROUTER_API_KEY  needed only on the first deploy, to write the host's .env
set -euo pipefail

[ $# -ge 2 ] || { sed -n '2,18p' "$0"; exit 2; }
HOST=$1; CMD=$2; shift 2
DIR=${NAS_DIR:-/volume1/docker/product-advisor}
PORT=${PORT:-8765}
if [ "$HOST" = local ]; then DOCKER=${DOCKER:-docker}; else DOCKER=${DOCKER:-sudo /usr/local/bin/docker}; fi
REPO=$(cd "$(dirname "$0")/.." && pwd)

# Run a shell command on the host; stdin is passed through. on_tty also allocates a terminal for a sudo prompt.
on() { if [ "$HOST" = local ]; then sh -c "$1"; else ssh "$HOST" "$1"; fi; }
on_tty() { if [ "$HOST" = local ]; then sh -c "$1"; else ssh -t "$HOST" "$1"; fi; }
compose() { echo "cd '$DIR' && $DOCKER compose --env-file deploy.env $*"; }

case "$CMD" in
deploy)
    PDF=${1:?usage: nas.sh HOST deploy BOOK.pdf}
    [ -f "$PDF" ] || { echo "no such file: $PDF" >&2; exit 1; }
    on "mkdir -p '$DIR/data'"
    echo "copying server files to $HOST:$DIR"
    (cd "$REPO" && git ls-files -co --exclude-standard mcp docker-compose.yml .env.example | COPYFILE_DISABLE=1 tar --no-xattrs -czf - -T -) | on "tar xzf - -C '$DIR'"
    echo "copying $(basename "$PDF")"
    on "cat > '$DIR/data/book.pdf'" < "$PDF"
    if ! on "test -s '$DIR/.env'"; then
        [ -n "${OPENROUTER_API_KEY:-}" ] || { echo "first deploy: set OPENROUTER_API_KEY (a key made for this server, with a credit limit)" >&2; exit 1; }
        printf 'OPENROUTER_API_KEY=%s\n' "$OPENROUTER_API_KEY" | on "umask 077 && cat > '$DIR/.env'"
        echo "wrote $DIR/.env"
    fi
    # the container runs as the owner of data/, so it can write keys, vectors, and summaries there
    on "cd '$DIR' && printf 'PUID=%s\nPGID=%s\nPORT=%s\n' \$(stat -c %u data) \$(stat -c %g data) $PORT > deploy.env"
    # Build and wait in one terminal session: sudo can ask for the password there and reuses it for the polls,
    # while a session without a terminal cannot ask at all.
    on_tty "$(compose up -d --build) || exit 1
echo 'waiting for the embedding index and section summaries (about three minutes on first start)'
i=0
while [ \$i -lt 120 ]; do
    logs=\$($(compose logs) 2>&1)
    case \"\$logs\" in *'section summaries ready'*) exit 0 ;; esac
    if $(compose ps -a) | grep -qE 'Restarting|Exited'; then
        echo 'the container stopped or is restarting; last log lines:' >&2
        printf '%s\n' \"\$logs\" | tail -20 >&2
        exit 1
    fi
    printf '%s\n' \"\$logs\" | grep 'not built yet' | tail -1 >&2
    i=\$((i + 1))
    sleep 5
done
echo 'not ready after 10 minutes; check: nas.sh $HOST logs' >&2
exit 1"
    echo "ready on 127.0.0.1:$PORT of $HOST (point the reverse proxy at it)"
    if on "test ! -s '$DIR/data/keys.json' || grep -q '^{}$' '$DIR/data/keys.json'"; then
        echo "no keys yet; issuing a full-scope key named owner (shown once):"
        on_tty "$(compose exec -T product-advisor python server.py issue-key owner --days 365 --scope full)"
    fi
    ;;
issue-key | renew-key | revoke-key | list-keys)
    args=""; [ $# -gt 0 ] && args=$(printf " '%s'" "$@")  # quoted for the host's shell; no empty argument when there are none
    on_tty "$(compose exec -T product-advisor python server.py "$CMD" "$args")"
    ;;
logs) on_tty "$(compose logs --tail 50)" ;;
status) on_tty "$(compose ps)" ;;
down) on_tty "$(compose down)" ;;
*) sed -n '2,18p' "$0"; exit 2 ;;
esac
