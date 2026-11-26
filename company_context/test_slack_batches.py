"""Slack publishes usable, explicit partial coverage before a large backfill completes."""
import asyncio
import json
import pytest
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
    monkeypatch.setattr(sync,'export_snapshot',lambda *a,**kw:snapshot.copy() | {'records':list(snapshot['records']), 'collection':dict(snapshot['collection'])})
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


@pytest.mark.parametrize("with_messages", [False, True])
def test_frozen_backfill_continues_after_empty_channel_revocation(tmp_path, monkeypatch, with_messages):
    import sqlite3
    from company_context.test_ingestion_review import SyncAPI
    from company_context.test_collect_slack import channel
    from company_context.collect_slack import save_messages
    setup_publication(tmp_path, monkeypatch)
    args=setup_sync(tmp_path, monkeypatch)
    settings=json.loads((tmp_path/'document-sources.json').read_text());settings['slack_batch_sources']=1
    (tmp_path/'document-sources.json').write_text(json.dumps(settings))
    with sqlite3.connect(args.bootstrap_database) as db:
        db.execute('INSERT INTO channels(id,info,done) VALUES (?,?,1)',('C002',json.dumps(channel()|{'id':'C002'})))
        save_messages(db,'C001',[{'ts':'17','user':'U1','reply_count':1,'text':'older'},{'ts':'18','user':'U1','reply_count':1,'text':'newer'},{'ts':'19','user':'U1','reply_count':1,'text':'newest'}],10,20)
        if with_messages:save_messages(db,'C002',[{'ts':'13','user':'U2','text':'withdrawn'}],10,20)
    visible=['C001','C002']
    class API(SyncAPI):
        def get(self,method,**params):
            if method=='conversations.list':return {'channels':[channel()|{'id':v} for v in visible]}
            return super().get(method,**params)
    monkeypatch.setattr(sync,'Slack',lambda _:API())
    monkeypatch.setattr(sync,'collect',lambda *a,**kw:True)
    asyncio.run(sync.synchronize(args))
    visible.remove('C002')
    asyncio.run(sync.synchronize(args))
    visible.append('C002')
    asyncio.run(sync.synchronize(args))
    value=json.loads((tmp_path/'current/snapshot.json').read_text())
    assert {v['source_id'] for v in value['records']} == {'slack:T1:C001:thread:17','slack:T1:C001:thread:18','slack:T1:C001:thread:19'}
    assert value['collection']['channel_ids'] == ['C001']
    assert value['collection']['access_excluded_channel_ids'] == ['C002']


def test_unchanged_records_report_current_verified_coverage_without_models(tmp_path,monkeypatch):
    from company_context.context import Corpus
    from company_context.server import create_app
    from company_context.evaluate import rpc
    from starlette.testclient import TestClient
    setup_publication(tmp_path,monkeypatch)
    args=setup_sync(tmp_path,monkeypatch)
    snapshot={'principal':'slack:T1:U1','records':[record('same')],'collection':{'channel_ids':['C001'],'completed_at':'2026-11-26T00:00:00Z','through':'2026-11-25T00:00:00Z','bot_only_messages_excluded':1}}
    monkeypatch.setattr(sync,'export_snapshot',lambda *a,**kw:json.loads(json.dumps(snapshot)))
    monkeypatch.setattr(sync,'collect',lambda *a,**kw:True)
    asyncio.run(sync.synchronize(args))
    snapshot['collection'].update(through='2026-11-26T00:00:00Z',bot_only_messages_excluded=10)
    monkeypatch.setattr(sync,'Models',lambda *a,**kw:(_ for _ in ()).throw(AssertionError('Metadata-only update must not call models')))
    asyncio.run(sync.synchronize(args))
    corpus=Corpus(json.loads((tmp_path/'current/snapshot.json').read_text()))
    headers={'Authorization':'Bearer '+args.mcp_token_file.read_text(),'Accept':'application/json, text/event-stream'}
    with TestClient(create_app(corpus,None,args.mcp_token_file,tmp_path/'access.json',tmp_path/'sync.json'),base_url='http://testserver/mcp',headers=headers) as client:
        result=rpc(client,'tools/call',{'name':'company_index_status','arguments':{}})
    assert result['coverage']['through']=='2026-11-26T00:00:00Z'
    assert result['coverage']['bot_only_messages_excluded']==10
