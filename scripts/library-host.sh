#!/usr/bin/env bash
# Activate a staged release on the existing host; accept only after real MCP checks.
set -euo pipefail
ACTION=${1:?activate or rollback}
VERSION=${2:?release version}
[[ "$VERSION" =~ ^three-books-[a-f0-9]{16}$ ]] || exit 2
STAGE="$PWD/staging/$VERSION"
exec > >(tee -a "$STAGE/activation.log") 2>&1
DOCKER=${DOCKER:-sudo /usr/local/bin/docker}
compose() { $DOCKER compose --env-file deploy.env "$@"; }

restore() {
    local backup=$1
    cp -R "$backup/mcp/." mcp/
    cp "$backup/docker-compose.yml" docker-compose.yml
    if [ -f "$backup/library-active.json" ]; then
        cp "$backup/library-active.json" data/library-active.json.tmp
        mv data/library-active.json.tmp data/library-active.json
    else
        rm -f data/library-active.json
    fi
    $DOCKER tag "$(cat "$backup/image-id")" "$(cat "$backup/image-tag")"
    compose up -d --no-build --force-recreate
    echo "ROLLED_BACK: $backup"
}

if [ "$ACTION" = rollback ]; then
    restore "$(cat "$STAGE/rollback-path")"
    exit
fi
[ "$ACTION" = activate ] || exit 2
test -s "$STAGE/verified.json"
python3 - "$STAGE/evaluation-passed.json" "$VERSION" "$STAGE" "$PWD" <<'PY'
import json,sys,pathlib
evaluation=json.load(open(sys.argv[1]))
assert evaluation['version']==sys.argv[2]
assert all(evaluation['summary'][mode]['passed']==evaluation['summary'][mode]['total']==15 for mode in ('mix','naive'))
ready=pathlib.Path(sys.argv[3])/'release'/'ready.json'
if not ready.exists():ready=pathlib.Path(sys.argv[4])/'data'/'libraries'/sys.argv[2]/'ready.json'
retrieval=json.loads(ready.read_text())['retrieval_version']
assert len(evaluation['rows'])==30 and all(row['passed'] and row['retrieval']['retrieval_version']==retrieval for row in evaluation['rows'])
PY
test ! -e "$STAGE/accepted.json" || { echo 'This release was already accepted'; exit 1; }
container=$(compose ps -q product-advisor)
test -n "$container"
BACKUP="$PWD/backups/$VERSION-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$BACKUP"
$DOCKER inspect --format '{{.Image}}' "$container" > "$BACKUP/image-id"
$DOCKER inspect --format '{{.Config.Image}}' "$container" > "$BACKUP/image-tag"
$DOCKER tag "$(cat "$BACKUP/image-id")" "product-advisor-rollback:$VERSION"
cp -R mcp "$BACKUP/mcp"
cp docker-compose.yml "$BACKUP/"
if [ -e data/library-active.json ]; then cp data/library-active.json "$BACKUP/"; fi
printf '%s\n' "$BACKUP" > "$STAGE/rollback-path"
success=false
trap 'if [ "$success" != true ]; then echo "Activation failed; restoring previous service"; restore "$BACKUP"; fi' EXIT
mkdir -p data/libraries
if [ ! -d "data/libraries/$VERSION" ]; then
    mv "$STAGE/release" "data/libraries/$VERSION"
fi
cp -R "$STAGE/code/mcp/." mcp/
cp "$STAGE/code/docker-compose.yml" docker-compose.yml
compose build product-advisor
printf '{"version":"%s"}\n' "$VERSION" > data/library-active.json.tmp
mv data/library-active.json.tmp data/library-active.json
rm -f "$STAGE/rejected.json"
compose up -d --no-build --force-recreate product-advisor
ready=false
for ((i=0;i<120;i++)); do
    if compose logs --tail 100 product-advisor 2>&1 | grep -q 'Book library index ready'; then ready=true; break; fi
    sleep 2
done
[ "$ready" = true ] || { echo 'Library startup did not become ready'; exit 1; }
echo "WAITING_FOR_REAL_MCP_ACCEPTANCE: $VERSION"
for ((i=0;i<150;i++)); do
    test ! -e "$STAGE/rejected.json" || { echo 'MCP acceptance failed'; exit 1; }
    if [ -e "$STAGE/accepted.json" ]; then
        success=true
        echo "DEPLOYMENT_ACCEPTED: $VERSION"
        exit
    fi
    sleep 2
done
echo 'No MCP acceptance within 300 seconds'
exit 1
