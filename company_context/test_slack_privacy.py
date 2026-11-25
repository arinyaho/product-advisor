"""Slack indexing excludes direct and group messages before any model calls."""

import asyncio

import pytest

from company_context.context import Corpus, build
from company_context.test_context import record


def source(channel="C001", **flags):
    return record(url=f"https://workspace.slack.com/archives/{channel}/p123", slack_conversation={"id": channel, "is_im": False, "is_mpim": False, "is_private": False, "is_member": False, **flags})


@pytest.mark.parametrize("item", [
    source("D001", is_im=True),
    source("G001", is_mpim=True, is_private=True, is_member=True),
    source("D001"),
    source(is_im=None),
    source(is_mpim=None),
    source(is_private=None),
    source(is_private=True, is_member=False),
    {**source(), "slack_conversation": None},
    {**source(), "slack_conversation": {"id": "C002", "is_im": False, "is_mpim": False, "is_private": False}},
    {**source(), "record_kind": "summary", "derived_from": ["https://workspace.slack.com/archives/D001/p123"]},
])
def test_unknown_or_private_message_provenance_is_rejected(item):
    with pytest.raises(ValueError, match="Slack|DM"):
        Corpus({"principal": "synthetic-reader", "records": [item]})


def test_accessible_channels_include_unjoined_public_and_joined_private():
    for item in [source(), source("G001", is_private=True, is_member=True)]:
        value = Corpus({"principal": "synthetic-reader", "records": [item]})
        assert value.records[item["source_id"]]["slack_conversation"] == item["slack_conversation"]


def test_dm_is_rejected_before_creating_a_snapshot_or_calling_models(tmp_path):
    directory = tmp_path / "snapshot"
    with pytest.raises(ValueError, match="Slack|DM"):
        asyncio.run(build({"principal": "synthetic-reader", "records": [source("D001", is_im=True)]}, directory, None))
    assert not directory.exists()
