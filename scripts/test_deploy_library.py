"""The host transaction must restore the old pointer and runtime on a build failure."""

import os
import json
from pathlib import Path
import subprocess
import hashlib
import importlib.util


def test_failed_build_restores_runtime_and_preserves_keys(tmp_path, bad_fixture=False, already_accepted=False):
    version = "three-books-0123456789abcdef"
    deployment = version + "-00000000000b"
    # An earlier deployment of the same release with other runtime code does not block this one.
    (tmp_path / "staging" / (version + "-00000000000a")).mkdir(parents=True)
    (tmp_path / "staging" / (version + "-00000000000a") / "accepted.json").write_text("{}")
    stage = tmp_path / "staging" / deployment
    (stage / "code/mcp").mkdir(parents=True)
    (stage / "release").mkdir()
    (stage / "verified.json").write_text("{}")
    fixture = json.dumps({"cases": [{"id": str(i)} for i in range(15)]}).encode()
    (stage / "cases.json").write_bytes(fixture)
    (stage / "evaluation-passed.json").write_text(json.dumps({"version": version, "fixture_sha256": "wrong" if bad_fixture else hashlib.sha256(fixture).hexdigest(), "summary": {mode: {"passed": 15, "total": 15} for mode in ("mix", "naive")}, "rows": [{"case": str(i), "mode": mode, "passed": True, "retrieval": {"retrieval_version": "test"}} for i in range(15) for mode in ("mix", "naive")]}))
    (stage / "release/ready.json").write_text(json.dumps({"retrieval_version": "test"}))
    (stage / "code/mcp/server.py").write_text("new runtime")
    (stage / "code/docker-compose.yml").write_text("new compose")
    if already_accepted:
        (stage / "accepted.json").write_text("{}")
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
 *"build --build-arg BOOK_LIBRARY_DEPLOYMENT=three-books-0123456789abcdef-00000000000b product-advisor"*) exit 7 ;;
esac
exit 0
''')
    docker.chmod(0o700)
    script = Path(__file__).with_name("library-host.sh")
    result = subprocess.run(["bash", str(script), "activate", deployment], cwd=tmp_path, env={**os.environ, "DOCKER": str(docker)}, capture_output=True, text=True)
    assert result.returncode != 0
    if bad_fixture or already_accepted:
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


def test_accepted_deployment_stops_before_mutation(tmp_path):
    test_failed_build_restores_runtime_and_preserves_keys(tmp_path, already_accepted=True)


def test_runtime_code_change_is_a_new_deployment():
    spec = importlib.util.spec_from_file_location("deploy_library", Path(__file__).with_name("deploy-library.py"))
    deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(deploy)
    inventory = {"code/mcp/server.py": {"sha256": "a"}, "library-host.sh": {"sha256": "b"}, "release/catalog.json": {"sha256": "c"}}
    changed_code = {**inventory, "code/mcp/server.py": {"sha256": "d"}}
    changed_release = {**inventory, "release/catalog.json": {"sha256": "e"}}
    assert deploy.runtime_id(changed_code) != deploy.runtime_id(inventory) == deploy.runtime_id(changed_release)


def test_stage_prints_runnable_accept_command_for_selected_root(tmp_path, monkeypatch, capsys):
    import shlex
    import sys

    spec = importlib.util.spec_from_file_location("deploy_library", Path(__file__).with_name("deploy-library.py"))
    deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(deploy)
    ssh = tmp_path / "ssh"
    ssh.write_text('#!/bin/sh\nshift\nexec sh -c "$1"\n')
    ssh.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    release = tmp_path / "release"
    release.mkdir()
    version = "three-books-0123456789abcdef"
    (release / "manifest.json").write_text(json.dumps({"version": version}))
    (release / "catalog.json").write_text("{}")
    (release / "ready.json").write_text(json.dumps({
        "retrieval_version": "test", "file_sha256": {"catalog.json": deploy.sha(release / "catalog.json")},
    }))
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps({"cases": [{"id": "a"}]}))
    evaluation = tmp_path / "report.json"
    evaluation.write_text(json.dumps({
        "version": version, "fixture_sha256": deploy.sha(cases), "retrieval_version": "test",
        "rows": [{"case": "a", "mode": mode, "passed": True} for mode in ("mix", "naive")],
    }))
    remote_root = tmp_path / "deploy here"
    deploy.stage("mock-host", str(remote_root), release, evaluation, cases)
    argv = shlex.split(capsys.readouterr().out.splitlines()[-1])
    assert argv[:2] == [sys.executable, str(Path(__file__).with_name("verify-live-library.py").resolve())]
    assert argv[argv.index("--deploy-dir") + 1] == str(remote_root)
    deployment = argv[argv.index("--deployment") + 1]
    assert (remote_root / "staging" / deployment / "verified.json").is_file()
