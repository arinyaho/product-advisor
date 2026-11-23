#!/usr/bin/env python3
"""Stage a sealed library and runtime files without credentials, keys, or build caches."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import tarfile
import tempfile


RUNTIME = ["Dockerfile", ".dockerignore", "requirements.txt", "server.py", "advisor_graph.py", "concept_map.json", "book_library.py", "library_index.py", "library_cli.py"]


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def stage(host, deploy_dir, release, evaluation, cases):
    root = Path(__file__).resolve().parent.parent
    manifest = json.loads((release / "manifest.json").read_text())
    version = manifest["version"]
    assert re.fullmatch(r"three-books-[a-f0-9]{16}", version)
    ready = json.loads((release / "ready.json").read_text())
    assert ready.get("file_sha256"), "Seal the index before staging"
    report = json.loads(evaluation.read_text())
    fixture = json.loads(cases.read_text())
    assert report["version"] == version and report["fixture_sha256"] == sha(cases)
    assert report["retrieval_version"] == ready["retrieval_version"]
    assert len(report["rows"]) == 2 * len(fixture["cases"]) and all(row["passed"] for row in report["rows"])
    assert {(row["case"], row["mode"]) for row in report["rows"]} == {(case["id"], mode) for case in fixture["cases"] for mode in ("mix", "naive")}
    files = {"code/mcp/" + name: root / "mcp" / name for name in RUNTIME}
    files["code/docker-compose.yml"] = root / "docker-compose.yml"
    files["library-host.sh"] = root / "scripts/library-host.sh"
    files["evaluation-passed.json"] = evaluation
    files["cases.json"] = cases
    for name in ("manifest.json", "catalog.json", "ready.json"):
        files["release/" + name] = release / name
    for folder in ("sources", "indices"):
        for path in sorted((release / folder).rglob("*")):
            if path.is_file() and not path.name.endswith((".lock", ".tmp")):
                files["release/" + str(path.relative_to(release))] = path
    for name, expected in ready["file_sha256"].items():
        assert sha(release / name) == expected, name
    inventory = {name: {"sha256": sha(path), "bytes": path.stat().st_size} for name, path in files.items()}
    remote = deploy_dir.rstrip("/") + "/staging/" + version
    subprocess.run(["ssh", host, "umask 077; mkdir -p " + shlex.quote(remote)], check=True)
    receiver = subprocess.Popen(["ssh", host, "tar xzf - -C " + shlex.quote(remote)], stdin=subprocess.PIPE)
    with tempfile.TemporaryDirectory() as temporary:
        receipt = Path(temporary) / "inventory.json"
        receipt.write_text(json.dumps({"version": version, "files": inventory}, indent=2))
        with tarfile.open(fileobj=receiver.stdin, mode="w|gz") as archive:
            for name, path in files.items():
                archive.add(path, arcname=name, recursive=False)
            archive.add(receipt, arcname="inventory.json")
        receiver.stdin.close()
        assert receiver.wait() == 0, "Artifact transfer failed; active service unchanged"
    verifier = '''import hashlib,json,pathlib,sys
p=pathlib.Path(sys.argv[1]); d=json.loads((p/'inventory.json').read_text())
for name,record in d['files'].items():
 f=p/name
 with f.open('rb') as s:
  h=hashlib.sha256()
  for block in iter(lambda:s.read(1024*1024),b''):h.update(block)
 assert f.stat().st_size==record['bytes'] and h.hexdigest()==record['sha256'],name
(p/'verified.json').write_text(json.dumps({'version':d['version'],'files':len(d['files'])}))
print(json.dumps({'staged':d['version'],'verified_files':len(d['files'])}))
'''
    subprocess.run(["ssh", host, "python3 - " + shlex.quote(remote)], input=verifier, text=True, check=True)
    command = "cd " + shlex.quote(deploy_dir) + " && bash staging/" + version + "/library-host.sh activate " + version
    print("Activation requires the NAS sudo credential in a terminal:")
    print(shlex.join(["ssh", "-t", host, command]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host")
    parser.add_argument("release", type=Path)
    parser.add_argument("--deploy-dir", default="/volume1/docker/product-advisor")
    parser.add_argument("--evaluation", required=True, type=Path)
    parser.add_argument("--cases", type=Path, default=Path(__file__).resolve().parent.parent / "eval/three_book_cases.json")
    args = parser.parse_args()
    stage(args.host, args.deploy_dir, args.release.resolve(), args.evaluation, args.cases)
