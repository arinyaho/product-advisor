"""Top-5 pages for every pooled question from one server version, saved as run_<name>.json.

    BOOK_PDF=... OPENROUTER_API_KEY=... python eval/pooled/run_system.py NAME [--server mcp] [--data-dir ...]
Server settings that vary between versions (such as CHUNK_WORDS in older ones) are read from the environment.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import ROOT, RESULTS, load_server  # noqa: E402

cli = argparse.ArgumentParser()
cli.add_argument("name")
cli.add_argument("--server", default=ROOT.parent / "mcp")
cli.add_argument("--data-dir", type=Path)
cli.add_argument("--out", type=Path, default=RESULTS / "pooled")
args = cli.parse_args()
server = load_server(args.server, args.data_dir or ROOT / ".cache" / "pooled" / args.name)
questions = json.loads((args.out / "questions.json").read_text())
run = {it["id"]: [{"page": r["page"], "section": r["section"]} for r in server.search(it["question"], 5, "full")["results"]]
       for it in questions}
(args.out / f"run_{args.name}.json").write_text(json.dumps(run, indent=1))
print(f"wrote run_{args.name}.json")
