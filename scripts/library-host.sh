#!/usr/bin/env bash
# Activate a staged release on the existing host; accept only after real MCP checks.
set -euo pipefail
ACTION=${1:?activate or rollback}
DEPLOYMENT=${2:?deployment printed by deploy-library.py}
[[ "$DEPLOYMENT" =~ ^three-books-[a-f0-9]{16}-[a-f0-9]{12}$ ]] || exit 2
VERSION=${DEPLOYMENT%-*}
STAGE="$PWD/staging/$DEPLOYMENT"
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
import hashlib,json,sys,pathlib
evaluation=json.load(open(sys.argv[1]))
assert evaluation['version']==sys.argv[2]
fixture=pathlib.Path(sys.argv[3])/'cases.json'
assert hashlib.sha256(fixture.read_bytes()).hexdigest()==evaluation['fixture_sha256'], 'Evaluation fixture mismatch'
cases=json.loads(fixture.read_text())['cases']
assert {(row['case'],row['mode']) for row in evaluation['rows']}=={(case['id'],mode) for case in cases for mode in ('mix','naive')}
assert all(evaluation['summary'][mode]['passed']==evaluation['summary'][mode]['total']==15 for mode in ('mix','naive'))
ready=pathlib.Path(sys.argv[3])/'release'/'ready.json'
if not ready.exists():ready=pathlib.Path(sys.argv[4])/'data'/'libraries'/sys.argv[2]/'ready.json'
retrieval=json.loads(ready.read_text())['retrieval_version']
assert len(evaluation['rows'])==30 and all(row['passed'] and row['retrieval']['retrieval_version']==retrieval for row in evaluation['rows'])
PY
test ! -e "$STAGE/accepted.json" || { echo 'This deployment was already accepted'; exit 1; }
container=$(compose ps -q product-advisor)
test -n "$container"
BACKUP="$PWD/backups/$DEPLOYMENT-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$BACKUP"
$DOCKER inspect --format '{{.Image}}' "$container" > "$BACKUP/image-id"
$DOCKER inspect --format '{{.Config.Image}}' "$container" > "$BACKUP/image-tag"
$DOCKER tag "$(cat "$BACKUP/image-id")" "product-advisor-rollback:$DEPLOYMENT"
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
compose build --build-arg "BOOK_LIBRARY_DEPLOYMENT=$DEPLOYMENT" product-advisor
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
echo "WAITING_FOR_REAL_MCP_ACCEPTANCE: $DEPLOYMENT"
for ((i=0;i<150;i++)); do
    test ! -e "$STAGE/rejected.json" || { echo 'MCP acceptance failed'; exit 1; }
    if [ -e "$STAGE/accepted.json" ]; then
        python3 - "$STAGE" "$DEPLOYMENT" <<'PY'
import json,pathlib,sys
stage=pathlib.Path(sys.argv[1])
accepted=json.loads((stage/'accepted.json').read_text())
evaluation=json.loads((stage/'evaluation-passed.json').read_text())
assert accepted['status']=='accepted' and accepted['version']==evaluation['version'] and accepted['deployment']==sys.argv[2]
assert accepted['fixture_sha256']==evaluation['fixture_sha256']
PY
        success=true
        echo "DEPLOYMENT_ACCEPTED: $DEPLOYMENT"
        exit
    fi
    sleep 2
done
echo 'No MCP acceptance within 300 seconds'
exit 1
