"""The host transaction must restore the old pointer and runtime on a build failure."""

import os
import json
from pathlib import Path
import subprocess
import hashlib


def test_failed_build_restores_runtime_and_preserves_keys(tmp_path, bad_fixture=False):
    version = "three-books-0123456789abcdef"
    stage = tmp_path / "staging" / version
    (stage / "code/mcp").mkdir(parents=True)
    (stage / "release").mkdir()
    (stage / "verified.json").write_text("{}")
    fixture = json.dumps({"cases": [{"id": str(i)} for i in range(15)]}).encode()
    (stage / "cases.json").write_bytes(fixture)
    (stage / "evaluation-passed.json").write_text(json.dumps({"version": version, "fixture_sha256": "wrong" if bad_fixture else hashlib.sha256(fixture).hexdigest(), "summary": {mode: {"passed": 15, "total": 15} for mode in ("mix", "naive")}, "rows": [{"case": str(i), "mode": mode, "passed": True, "retrieval": {"retrieval_version": "test"}} for i in range(15) for mode in ("mix", "naive")]}))
    (stage / "release/ready.json").write_text(json.dumps({"retrieval_version": "test"}))
    (stage / "code/mcp/server.py").write_text("new runtime")
    (stage / "code/docker-compose.yml").write_text("new compose")
    (tmp_path / "mcp").mkdir()
    (tmp_path / "mcp/server.py").write_text("old runtime")
    (tmp_path / "docker-compose.yml").write_text("old compose")
    (tmp_path / "deploy.env").write_text("")
    (tmp_path / "data").mkdir()
    (tmp_path / "data/keys.json").write_text("preserve-key-store")
    (tmp_path / "data/library-active.json").write_text('{"version":"old"}')
    docker = tmp_path / "docker"
    docker.write_text('''#!/bin/sh
case "$*" in
 *"ps -q"*) echo container ;;
 *"inspect --format {{.Image}}"*) echo sha256:old ;;
 *"inspect --format {{.Config.Image}}"*) echo old-tag ;;
 *"build product-advisor"*) exit 7 ;;
esac
exit 0
''')
    docker.chmod(0o700)
    script = Path(__file__).with_name("library-host.sh")
    result = subprocess.run(["bash", str(script), "activate", version], cwd=tmp_path, env={**os.environ, "DOCKER": str(docker)}, capture_output=True, text=True)
    assert result.returncode != 0
    if bad_fixture:
        assert "ROLLED_BACK:" not in result.stdout
        assert not (tmp_path / "backups").exists()
        assert (tmp_path / "mcp/server.py").read_text() == "old runtime"
        return
    assert "ROLLED_BACK:" in result.stdout
    assert (tmp_path / "mcp/server.py").read_text() == "old runtime"
    assert (tmp_path / "docker-compose.yml").read_text() == "old compose"
    assert (tmp_path / "data/library-active.json").read_text() == '{"version":"old"}'
    assert (tmp_path / "data/keys.json").read_text() == "preserve-key-store"


def test_changed_evaluation_fixture_stops_before_mutation(tmp_path):
    test_failed_build_restores_runtime_and_preserves_keys(tmp_path, bad_fixture=True)
