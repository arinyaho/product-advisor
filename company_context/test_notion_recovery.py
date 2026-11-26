"""Notion API omissions and dependency-complete page publication regressions."""

import asyncio
import json

import httpx
import pytest

from company_context import document_sync
from company_context.collect_notion import collect, enqueue, open_store, retain
from company_context.document_sources import export_capture
from company_context.test_document_sync import READER, doc, setup_publication


@pytest.mark.parametrize('kind,message,reason', [
    ('children', 'Block type external_object_instance_page is not supported via the API.', 'api_unsupported_block'),
    ('property', 'Invalid relations found in rollup.', 'invalid_rollup_relations'),
    ('property', 'notion://tasks/status_property is not a property that exists.', 'invalid_rollup_relations'),
    ('children', 'Invalid request parameter.', None),
    ('property', 'Block type external_object_instance_page is not supported via the API.', None),
])
def test_only_recognized_endpoint_specific_validation_errors_are_omissions(tmp_path, kind, message, reason):
    db = open_store(tmp_path / 'capture.sqlite', {'workspace_id': 'W'})
    with db:
        db.execute('UPDATE tasks SET done=1')
        enqueue(db, kind, 'p/r' if kind == 'property' else 'p')
    class API:
        def request(self, *a, **kw):
            response = httpx.Response(400, json={'code': 'validation_error', 'message': message}, request=httpx.Request('GET', 'https://api.notion.com/v1/test'))
            response.raise_for_status()
    collect(API(), db)
    assert db.execute('SELECT done,error FROM tasks WHERE kind=?', (kind,)).fetchone() == ((1, None) if reason else (0, '400'))
    assert set(db.execute('SELECT kind,id,reason FROM omissions')) == ({(kind, 'p/r' if kind == 'property' else 'p', reason)} if reason else set())
    db.close()


def test_small_notion_access_check_reads_each_required_page_and_synced_block(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from company_context import document_sources
    from company_context.test_document_sync import cache
    cache(tmp_path, ['p1','p2','block:origin'])
    row = doc(); row['provider_scope']['dependencies'] = ['block:origin']
    calls = []
    class API:
        client = SimpleNamespace(close=lambda: None)
        def __init__(self, *a): pass
        def request(self, method, path, **kwargs):
            calls.append((method,path))
            if path == 'users/me': return {'id':'bot1','bot':{'workspace_id':'W'}}
            assert method == 'GET' and path in ('pages/p1','pages/p2','blocks/origin')
            if path == 'pages/p2':
                httpx.Response(404,request=httpx.Request('GET','https://api.notion.com/v1/test')).raise_for_status()
            return {'archived':False, 'in_trash':False}
    monkeypatch.setattr(document_sources,'Notion',API)
    settings = {'principal':READER,'providers':{'notion':{'token_file':'synthetic','workspace_id':'W','connection_id':'bot1'}}}
    result = document_sources.refresh_document_access(tmp_path, settings, [{'records':[row,doc(identity='p2')]}])
    assert set(calls) == {('GET','users/me'),('GET','pages/p1'),('GET','pages/p2'),('GET','blocks/origin')}
    assert set(result['providers']['notion']['resource_ids']) == {'p1','block:origin'}


def test_large_notion_access_check_keeps_paginated_search_and_missing_page_reads(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from company_context import document_sources
    calls = []
    class API:
        client = SimpleNamespace(close=lambda: None)
        def __init__(self, *a): pass
        def request(self, method, path, **kwargs):
            calls.append((method,path))
            if path == 'users/me': return {'id':'bot1','bot':{'workspace_id':'W'}}
            if path == 'search':
                cursor = kwargs['json'].get('start_cursor')
                return {'results':[{'id':'p'+str(i)} for i in range(50 if cursor else 0, 100 if cursor else 50)], 'next_cursor':None if cursor else 'next'}
            assert path == 'pages/p100'
            return {'archived':True}
    monkeypatch.setattr(document_sources,'Notion',API)
    settings = {'principal':READER,'providers':{'notion':{'token_file':'synthetic','workspace_id':'W','connection_id':'bot1'}}}
    result = document_sources.refresh_document_access(tmp_path,settings,[{'records':[doc(identity='p'+str(i)) for i in range(101)]}])
    assert calls == [('GET','users/me'),('POST','search'),('POST','search'),('GET','pages/p100')]
    assert set(result['providers']['notion']['resource_ids']) == {'p'+str(i) for i in range(100)}


def test_partial_export_requires_every_rendered_dependency_and_stable_source_time(tmp_path):
    path = tmp_path / 'capture.sqlite'
    db = open_store(path, {'workspace_id': 'W', 'connection_id': 'bot1'})
    settings = {'database': str(path), 'workspace_id': 'W', 'connection_id': 'bot1', 'allow_partial': True}
    with db:
        for identity in ('ready', 'nested', 'property', 'synced', 'unread'):
            retain(db, {'object': 'page', 'id': identity, 'properties': {}}, ('search', 'all'), 0)
        retain(db, {'object': 'block', 'id': 'toggle', 'has_children': True, 'type': 'toggle'}, ('children', 'nested'), 0)
        retain(db, {'object': 'block', 'id': 'synced-block', 'type': 'synced_block', 'synced_block': {'synced_from': {'block_id': 'origin'}}}, ('children', 'synced'), 0)
        enqueue(db, 'property', 'property/value')
        db.execute("UPDATE tasks SET done=1 WHERE kind IN ('page','children') AND id IN ('ready','nested','property','synced')")
    first = export_capture('notion', settings, READER)
    assert {r['source_id'] for r in first['records']} == {'notion:W:ready'}
    assert first['collection']['capture_complete'] is False
    assert first['collection']['pending_pages'] == 4
    assert 'page_dependencies_incomplete' not in first['collection']['export_omissions']
    stable = first['records'][0]
    with db:
        db.execute("UPDATE tasks SET done=1 WHERE kind='children' AND id='toggle'")
        retain(db, {'object': 'block', 'id': 'answer', 'type': 'paragraph', 'paragraph': {'rich_text': [{'plain_text': 'Nested decision'}]}}, ('children', 'toggle'), 0)
    second = export_capture('notion', settings, READER)
    assert {r['source_id'] for r in second['records']} == {'notion:W:ready', 'notion:W:nested'}
    assert next(r for r in second['records'] if r['source_id'] == 'notion:W:ready') == stable
    assert 'Nested decision' in next(r for r in second['records'] if r['source_id'] == 'notion:W:nested')['text']
    with db:
        db.execute('UPDATE tasks SET done=1,error=NULL')
        db.execute("INSERT INTO metadata VALUES ('finished_at','2026-11-26T00:00:00Z')")
    final = export_capture('notion', settings, READER)
    assert next(r for r in final['records'] if r['source_id'] == 'notion:W:ready') == stable
    assert final['collection']['capture_complete'] is True
    db.close()


def test_new_partial_capture_cannot_remove_previously_published_provider_sources(tmp_path, monkeypatch):
    settings, calls = setup_publication(tmp_path, monkeypatch)
    settings['providers'] = {'notion': {}}
    previous = tmp_path / 'old'; previous.mkdir()
    old = {'principal': READER, 'records': [doc()], 'collection': {'managed_sources': {'notion': {'capture_id': 'old'}}}}
    (previous / 'snapshot.json').write_text(json.dumps(old)); (tmp_path / 'current').symlink_to(previous)
    monkeypatch.setattr(document_sync, 'export_capture', lambda *a: {'principal': READER, 'records': [doc(identity='p2')], 'collection': {'capture_id': 'new', 'capture_complete': False}})
    asyncio.run(document_sync.synchronize(tmp_path, settings))
    assert json.loads((tmp_path / 'current/snapshot.json').read_text()) == old
    assert calls == []
    assert json.loads((tmp_path / 'document-ingestion.json').read_text())['notion']['phase'] == 'new_capture_in_progress'


def test_partial_metadata_progress_and_completion_do_not_repeat_extraction(tmp_path, monkeypatch):
    from company_context.context import Corpus
    from company_context.sync import reported_coverage
    settings, calls = setup_publication(tmp_path, monkeypatch)
    settings['providers'] = {'notion': {}}
    coverage = {'capture_id': 'capture', 'capture_complete': False, 'pending_pages': 2}
    monkeypatch.setattr(document_sync, 'export_capture', lambda *a: {'principal': READER, 'records': [doc()], 'collection': dict(coverage)})
    asyncio.run(document_sync.synchronize(tmp_path, settings))
    assert json.loads((tmp_path / 'document-ingestion.json').read_text())['notion']['phase'] == 'waiting_for_more_pages'
    count = len(calls)
    for pending in (1, 0):
        coverage.update(pending_pages=pending, capture_complete=pending == 0)
        asyncio.run(document_sync.synchronize(tmp_path, settings))
        old = json.loads((tmp_path / 'current/snapshot.json').read_text())
        observed = reported_coverage(old, Corpus(old).version, tmp_path / 'sync.json')['managed_sources']['notion']
        assert observed['pending_pages'] == pending
        assert observed['index_complete'] == (pending == 0)
        assert len(calls) == count


def test_live_export_reader_does_not_block_collector_commits(tmp_path):
    import sqlite3
    path = tmp_path / 'capture.sqlite'
    writer = open_store(path, {'workspace_id': 'W'})
    reader = sqlite3.connect(path, timeout=0)
    writer.execute('PRAGMA busy_timeout=0')
    reader.execute('BEGIN')
    reader.execute('SELECT * FROM metadata').fetchall()
    try:
        with writer: enqueue(writer, 'page', 'new')
        reader.rollback()
        assert reader.execute("SELECT id FROM tasks WHERE kind='page'").fetchall() == [('new',)]
    finally:
        reader.close(); writer.close()


def test_operator_validation_retry_retains_positions_and_unknown_failures(tmp_path):
    from company_context import collect_notion
    db = open_store(tmp_path / 'capture.sqlite', {'workspace_id': 'W'})
    with db:
        db.execute('UPDATE tasks SET done=1')
        for identity, error in [('known', '400'), ('unknown', '400'), ('other', '503')]:
            enqueue(db, 'children', identity)
            db.execute("UPDATE tasks SET cursor='next',consumed=100,error=? WHERE id=?", (error, identity))
    assert collect_notion.retry_validation_errors(db) == 2
    assert set(db.execute('SELECT id,cursor,previous_error FROM operator_retries')) == {('known','next','400'), ('unknown','next','400')}
    calls = []
    class API:
        def request(self, method, path, **kwargs):
            calls.append((path, kwargs['params'].get('start_cursor')))
            message = 'Block type external_object_instance_page is not supported via the API.' if '/known/' in path else 'Invalid request parameter.'
            httpx.Response(400, json={'code':'validation_error', 'message':message}, request=httpx.Request('GET','https://api.notion.com/v1/test')).raise_for_status()
    collect(API(), db)
    assert calls == [('blocks/known/children','next'), ('blocks/unknown/children','next')]
    assert set(db.execute("SELECT id,consumed,error FROM tasks WHERE kind='children'")) == {('known',100,None), ('unknown',100,'400'), ('other',100,'503')}
    db.close()


def test_slack_publication_preserves_verified_notion_completion(tmp_path, monkeypatch):
    from company_context import sync
    from company_context.test_ingestion_review import setup_sync
    from company_context.test_document_sync import cache
    old = {'principal':READER, 'records':[doc()], 'collection':{'managed_sources':{'notion':{'capture_id':'capture','index_complete':False}}}}
    args = setup_sync(tmp_path, monkeypatch, old=old, messages=[{'ts':'12','text':'New evidence'}])
    cache(tmp_path)
    verified = {'managed_sources':{'notion':{'capture_id':'capture','index_complete':True}}}
    sync.write_json(tmp_path/'sync.json', {'phase':'unchanged','coverage_snapshot':sync.digest(old),'verified_collection':verified})
    observed = []
    async def publish(args, snapshot, *a): observed.append(snapshot)
    monkeypatch.setattr(sync, 'publish_snapshot', publish)
    asyncio.run(sync.synchronize(args))
    assert observed[0]['collection']['managed_sources'] == verified['managed_sources']


def test_withheld_page_metadata_needs_retained_body_not_a_generated_title(tmp_path):
    path = tmp_path/'capture.sqlite'
    db = open_store(path, {'workspace_id':'W','connection_id':'bot1'})
    with db:
        for identity in ('empty','safe'):
            retain(db, {'object':'page','id':identity,'properties':{},'text':'sk-'+'s'*24}, ('search','all'), 0)
            retain(db, {'object':'response_metadata','id':'children:'+identity+':','response':{'object':'list'}}, ('children',identity), -1)
        retain(db, {'object':'block','id':'body','type':'paragraph','paragraph':{'rich_text':[{'plain_text':'Safe retained decision'}]}}, ('children','safe'), 0)
        db.execute('UPDATE tasks SET done=1')
    result = export_capture('notion', {'database':str(path),'workspace_id':'W','connection_id':'bot1','allow_partial':True}, READER)
    assert {r['source_id'] for r in result['records']} == {'notion:W:safe'}
    assert 'Safe retained decision' in result['records'][0]['text']
    assert result['collection']['empty_withheld_pages'] == 1
    assert 'no_retained_content' not in result['collection']['export_omissions']
    db.close()


def test_unavailable_page_does_not_block_others_or_starve_later_batches(tmp_path, monkeypatch):
    from company_context import document_sources
    from company_context.test_document_sync import cache
    settings, calls = setup_publication(tmp_path, monkeypatch)
    settings['providers'] = {'notion': {}}
    rows = [doc()]
    monkeypatch.setattr(document_sync, 'export_capture', lambda *a: {'principal': READER, 'records': list(rows), 'collection': {'capture_complete': True}})
    asyncio.run(document_sync.synchronize(tmp_path, settings))
    allowed = ['p1', 'p2']
    def refresh(*a): cache(tmp_path, allowed)
    monkeypatch.setattr(document_sync, 'refresh_document_access', refresh)
    monkeypatch.setattr(document_sources, 'refresh_document_access', refresh)
    rows.extend([doc(identity='p2'), doc(identity='p3')])
    before = len(calls)
    asyncio.run(document_sync.synchronize(tmp_path, settings))
    assert len(calls) == before  # The entire first candidate was denied; no paid build.
    assert {r['source_id'] for r in json.loads((tmp_path/'current/snapshot.json').read_text())['records']} == {'p1'}
    status = json.loads((tmp_path/'document-ingestion.json').read_text())['notion']
    assert status['access_deferred_sources'] == 1
    assert status['batch_sources'] == 0
    asyncio.run(document_sync.synchronize(tmp_path, settings))
    assert {r['source_id'] for r in json.loads((tmp_path/'current/snapshot.json').read_text())['records']} == {'p1','p2'}
    allowed.append('p3')
    asyncio.run(document_sync.synchronize(tmp_path, settings))
    final = json.loads((tmp_path/'current/snapshot.json').read_text())
    assert {r['source_id'] for r in final['records']} == {'p1','p2','p3'}
    assert final['collection']['managed_sources']['notion']['index_complete'] is True
    assert json.loads((tmp_path/'sync.json').read_text())['document_access_deferred'] == {}


def test_denied_only_batch_yields_to_other_document_provider(tmp_path, monkeypatch):
    from company_context import document_sources
    from company_context.test_document_sync import cache
    settings, calls = setup_publication(tmp_path, monkeypatch)
    rows = {'notion': [doc()], 'drive': []}
    monkeypatch.setattr(document_sync, 'export_capture', lambda p, *a: {'principal': READER, 'records': rows[p], 'collection': {'capture_complete': True}})
    asyncio.run(document_sync.synchronize(tmp_path, settings))
    state_path = tmp_path/'sync.json'
    state = json.loads(state_path.read_text()); state['next_document_provider'] = 'notion'; state_path.write_text(json.dumps(state))
    rows['notion'].append(doc(identity='p2')); rows['drive'].append(doc('drive', 'f1'))
    def refresh(*a): cache(tmp_path, ['p1', 'f1'])
    monkeypatch.setattr(document_sync, 'refresh_document_access', refresh)
    monkeypatch.setattr(document_sources, 'refresh_document_access', refresh)
    asyncio.run(document_sync.synchronize(tmp_path, settings))
    asyncio.run(document_sync.synchronize(tmp_path, settings))
    assert {r['source_id'] for r in json.loads((tmp_path/'current/snapshot.json').read_text())['records']} == {'p1','f1'}


def test_retained_revocation_stays_in_remaining_coverage(tmp_path, monkeypatch):
    from company_context import document_sources
    from company_context.test_document_sync import cache
    settings, calls = setup_publication(tmp_path, monkeypatch)
    settings['providers'] = {'notion': {}}
    rows = [doc()]
    monkeypatch.setattr(document_sync, 'export_capture', lambda *a: {'principal': READER, 'records': rows, 'collection': {'capture_complete': True}})
    asyncio.run(document_sync.synchronize(tmp_path, settings))
    rows.append(doc(identity='p2'))
    def refresh(*a): cache(tmp_path, ['p2'])
    monkeypatch.setattr(document_sync, 'refresh_document_access', refresh)
    monkeypatch.setattr(document_sources, 'refresh_document_access', refresh)
    asyncio.run(document_sync.synchronize(tmp_path, settings))
    final = json.loads((tmp_path/'current/snapshot.json').read_text())
    assert {r['source_id'] for r in final['records']} == {'p2'}
    status = json.loads((tmp_path/'document-ingestion.json').read_text())['notion']
    assert status['indexed_sources'] == 1
    assert status['remaining_sources'] == 1
    assert status['phase'] == 'indexing'
    assert final['collection']['managed_sources']['notion']['index_complete'] is False


def test_unchanged_import_reconciles_revocation_when_other_provider_advances(tmp_path, monkeypatch):
    from company_context import document_sources
    from company_context.test_document_sync import cache
    settings, calls = setup_publication(tmp_path, monkeypatch)
    rows = {'notion': [doc()], 'drive': []}
    monkeypatch.setattr(document_sync, 'export_capture', lambda p, *a: {'principal': READER, 'records': rows[p], 'collection': {'capture_complete': True}})
    asyncio.run(document_sync.synchronize(tmp_path, settings))
    rows['drive'].append(doc('drive', 'f1'))
    def refresh(*a): cache(tmp_path, ['f1'])
    monkeypatch.setattr(document_sync, 'refresh_document_access', refresh)
    monkeypatch.setattr(document_sources, 'refresh_document_access', refresh)
    asyncio.run(document_sync.synchronize(tmp_path, settings))
    assert {r['source_id'] for r in json.loads((tmp_path/'current/snapshot.json').read_text())['records']} == {'f1'}


def test_inventory_refresh_includes_preflight_and_current_under_its_lock(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from company_context import document_sources
    (tmp_path/'document-preflight.json').write_text(json.dumps({'records': [doc(identity='p2')]}))
    calls = []
    class API:
        client = SimpleNamespace(close=lambda: None)
        def __init__(self, *a): pass
        def request(self, method, path, **kwargs):
            calls.append(path)
            if path == 'users/me': return {'id':'bot1', 'bot':{'workspace_id':'W'}}
            return {'archived':False}
    monkeypatch.setattr(document_sources, 'Notion', API)
    settings = {'principal':READER, 'providers':{'notion':{'token_file':'synthetic','workspace_id':'W','connection_id':'bot1'}}}
    result = document_sources.refresh_document_access(tmp_path, settings, [{'records':[doc()]}])
    assert set(calls) == {'users/me','pages/p1','pages/p2'}
    assert set(result['providers']['notion']['resource_ids']) == {'p1','p2'}
