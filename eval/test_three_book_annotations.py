"""A source-reviewed revision preserves every original question and negative case."""

import hashlib
import json
from pathlib import Path


def test_v2_only_adds_the_five_audited_sources():
    root = Path(__file__).parent
    original = (root / "three_book_cases.json").read_bytes()
    assert hashlib.sha256(original).hexdigest() == "2ee407af9803cd52948dacaa0cfff806bd985add81e5136ea8d41226187001dd"
    v1, v2 = json.loads(original), json.loads((root / "three_book_cases_v2.json").read_text())
    revision = v2["annotation_revision"]
    assert revision["parent_sha256"] == hashlib.sha256(original).hexdigest()
    expected_additions = {
        "src-cbbdad9d16fdfc2a4d49d83e", "src-e5422b82e8066ebee7dc67a5",
        "src-bb147357f161ca364defb8f7", "src-32aa956671106b45b231b880",
        "src-60b5110ea889b84511c82383",
    }
    assert {row["chunk_id"] for row in revision["additions"]} == expected_additions
    assert len(v1["cases"]) == len(v2["cases"]) == 15
    for old, new in zip(v1["cases"], v2["cases"]):
        if old["id"] != "all-pilot-ko":
            assert old == new
        else:
            assert {k: v for k, v in old.items() if k != "expected_chunk_ids"} == {k: v for k, v in new.items() if k != "expected_chunk_ids"}
            assert new["expected_chunk_ids"][:len(old["expected_chunk_ids"])] == old["expected_chunk_ids"]
            assert set(new["expected_chunk_ids"]) - set(old["expected_chunk_ids"]) == expected_additions
