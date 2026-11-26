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


@pytest.mark.parametrize("access_only", [False, True])
@pytest.mark.parametrize("with_messages", [False, True])
def test_frozen_backfill_continues_after_empty_channel_revocation(tmp_path, monkeypatch, with_messages, access_only):
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
    if access_only:
        sync.refresh_access(API(), tmp_path/'access.json')
        visible.append('C002')
        sync.refresh_access(API(), tmp_path/'access.json')
    asyncio.run(sync.synchronize(args))
    if 'C002' not in visible:visible.append('C002')
    asyncio.run(sync.synchronize(args))
    value=json.loads((tmp_path/'current/snapshot.json').read_text())
    assert {v['source_id'] for v in value['records']} == {'slack:T1:C001:thread:17','slack:T1:C001:thread:18','slack:T1:C001:thread:19'}
    assert value['collection']['channel_ids'] == ['C001']
    assert value['collection']['access_excluded_channel_ids'] == ['C002']
    def fresh_collection(api, db, since, until, previous=None):
        assert previous is None
        raise RuntimeError('fresh provider reads required')
    monkeypatch.setattr(sync,'collect',fresh_collection)
    with pytest.raises(RuntimeError,match='fresh provider reads required'):
        asyncio.run(sync.synchronize(args))


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


def test_document_publication_inherits_latest_verified_slack_coverage(tmp_path,monkeypatch):
    from company_context import document_sync
    from company_context.test_document_sync import doc
    settings,_=setup_publication(tmp_path,monkeypatch)
    settings['providers']={'drive':{}}
    old={'principal':'slack:T1:U1','records':[record('same')],'collection':{'through':'2026-11-24T00:00:00Z','completed_at':'2026-11-24T00:00:00Z'}}
    previous=tmp_path/'old';previous.mkdir();(previous/'snapshot.json').write_text(json.dumps(old));(tmp_path/'current').symlink_to(previous)
    (tmp_path/'sync.json').write_text(json.dumps({'phase':'unchanged','coverage_snapshot':sync.digest(old),'verified_collection':{'through':'2026-11-26T00:00:00Z','completed_at':'2026-11-26T00:00:00Z','bot_only_messages_excluded':10}}))
    monkeypatch.setattr(document_sync,'export_capture',lambda *a:{'principal':'slack:T1:U1','records':[doc('drive','f1')],'collection':{'completed_at':'2026-11-26T00:00:00Z'}})
    observed=[]
    async def publish(args,snapshot,*a):observed.append(snapshot)
    monkeypatch.setattr(document_sync,'publish_snapshot',publish)
    asyncio.run(document_sync.synchronize(tmp_path,settings))
    assert observed[0]['collection']['through']=='2026-11-26T00:00:00Z'
    assert observed[0]['collection']['bot_only_messages_excluded']==10


def test_known_revocation_after_export_blocks_dispatch_publication_and_reads(tmp_path):
    from datetime import datetime,timezone
    from company_context.context import Corpus
    corpus=Corpus({'principal':'slack:T1:U1','records':[record('stale')],'collection':{'slack_capture_started_at':'2026-11-24T00:00:00Z'}})
    access=tmp_path/'access.json'
    access.write_text(json.dumps({'principal':'slack:T1:U1','channel_ids':['C001'],'checked_at':datetime.now(timezone.utc).isoformat(),'revoked_channels':{'C001':'2026-11-25T00:00:00Z'}}))
    assert sync.availability(corpus,access)=='blocked_observed_channel_revocation'
    with pytest.raises(ValueError,match='access'):
        sync.require_access(corpus,access)


def test_revocation_discards_pending_checkpoints_without_deleting_archive(tmp_path,monkeypatch):
    import sqlite3
    args=setup_sync(tmp_path,monkeypatch,messages=[{'ts':'12','text':'old body','user':'U1'}])
    with sqlite3.connect(args.bootstrap_database) as db:
        db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',('capture_started_at','2026-11-24T00:00:00Z'))
    (tmp_path/'sync.json').write_text(json.dumps({'phase':'collecting','pending_database':str(args.bootstrap_database),'pending_until':20}))
    def access(*a):
        value={'principal':'slack:T1:U1','channel_ids':['C001'],'revoked_channels':{'C001':'2026-11-25T00:00:00Z'}}
        return value
    monkeypatch.setattr(sync,'refresh_access',access)
    observed=[]
    def collect(api,db,since,until,previous=None):
        observed.append(db.execute('PRAGMA database_list').fetchone()[2])
        assert db.execute('SELECT count(*) FROM messages').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM channels').fetchone()[0]==0
        raise RuntimeError('new capture started')
    monkeypatch.setattr(sync,'collect',collect)
    with pytest.raises(RuntimeError,match='new capture started'):
        asyncio.run(sync.synchronize(args))
    assert observed != [str(args.bootstrap_database)]
    with sqlite3.connect(args.bootstrap_database) as db:
        assert db.execute('SELECT ts FROM messages').fetchall()==[('12',)]
