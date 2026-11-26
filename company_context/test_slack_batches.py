"""Slack publishes usable, explicit partial coverage before a large backfill completes."""
import asyncio
import json
from company_context import sync
from company_context.test_context import record
from company_context.test_document_sync import setup_publication
from company_context.test_ingestion_review import setup_sync


def test_slack_batches_publish_exact_membership_and_reuse_frozen_capture(tmp_path, monkeypatch):
    setup_publication(tmp_path, monkeypatch)
    args = setup_sync(tmp_path, monkeypatch)
    settings = json.loads((tmp_path / 'document-sources.json').read_text())
    settings.update(slack_batch_sources=1, slack_batch_characters=10000)
    (tmp_path / 'document-sources.json').write_text(json.dumps(settings))
    older = record('older', occurred_at='2026-11-24T00:00:00Z')
    newer = record('newer', occurred_at='2026-11-25T00:00:00Z')
    snapshot = {'principal':'slack:T1:U1', 'records':[older,newer], 'collection':{'kind':'slack_channel_capture','channel_ids':['C001'],'completed_at':'2026-11-26T00:00:00Z'}}
    monkeypatch.setattr(sync,'export_snapshot',lambda *a:snapshot.copy() | {'records':list(snapshot['records']), 'collection':dict(snapshot['collection'])})
    asyncio.run(sync.synchronize(args))
    first=json.loads((tmp_path/'current/snapshot.json').read_text())
    assert {r['source_id'] for r in first['records']} == {'newer'}
    assert first['collection']['slack_indexing'] == {'available_sources':2,'indexed_sources':1,'complete':False}
    def no_recapture(*a,**kw): raise AssertionError('Frozen backfill must not repeat capture')
    monkeypatch.setattr(sync,'collect',no_recapture)
    asyncio.run(sync.synchronize(args))
    second=json.loads((tmp_path/'current/snapshot.json').read_text())
    assert {r['source_id'] for r in second['records']} == {'older','newer'}
    assert second['collection']['slack_indexing'] == {'available_sources':2,'indexed_sources':2,'complete':True}
    assert not json.loads((tmp_path/'sync.json').read_text()).get('slack_batch_database')
