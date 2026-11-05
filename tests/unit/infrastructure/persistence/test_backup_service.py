from contextlib import closing
import json
from pathlib import Path
import sqlite3
import zipfile

import pytest

from infrastructure.persistence.backup_service import BackupService, BackupInvalid, BackupBusy, validate_version, file_lock
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.operation_journal import OperationJournal


@pytest.fixture
def service(tmp_path):
    main = tmp_path/'main.sqlite3'
    db = DatabaseConnection(str(main))
    db.execute("INSERT INTO novels(id,title,slug) VALUES ('book','fixture','book')")
    db.execute("INSERT INTO chapters(id,novel_id,number,title,content) VALUES ('ch','book',1,'fixture','original text')")
    db.commit()
    journal = OperationJournal(main.with_suffix('.operations.sqlite3'))
    request = journal.reserve('book','llm','fixture',100,50)
    journal.finish(request,12,4)
    backup = BackupService(main)
    backup.fixture_db = db
    yield backup
    db.close_all()


def test_restore_refuses_live_database_lease(service):
    created = service.create()
    with pytest.raises(BackupBusy):
        service.restore(service.path(created['id']))


def offline(service):
    # Fixture's application connection must be closed before a restore.
    service.fixture_db.close_all()


def read_pair(service):
    with closing(sqlite3.connect(service.main)) as conn, conn:
        text = conn.execute("SELECT content FROM chapters WHERE id='ch'").fetchone()[0]
    with closing(sqlite3.connect(service.operations)) as conn, conn:
        calls = conn.execute('SELECT count(*) FROM calls').fetchone()[0]
    return text,calls


def change_pair(service):
    with closing(sqlite3.connect(service.main)) as conn, conn:
        conn.execute("UPDATE chapters SET content='newer text'")
    journal = OperationJournal(service.operations)
    journal.finish(journal.reserve('book','llm','fixture',100,50),30,10)


def test_restore_reinstates_both_files_and_retains_safety_backup(service,monkeypatch):
    archive = service.path(service.create()['id'])
    change_pair(service)
    offline(service)
    result = service.restore(archive)
    assert read_pair(service) == ('original text',1)
    assert service.inspect(service.path(result['safety_backup_id']))['counts']['operations']['calls'] == 2
    assert not service.pending.exists()
    with closing(sqlite3.connect(service.main)) as conn, conn:
        assert conn.execute('SELECT autopilot_status FROM novels').fetchone()[0] == 'stopped'
    from infrastructure.persistence.backup_service import restored_index_generation
    generation = restored_index_generation(service.main)
    assert generation and len(generation) == 32
    # Every restored snapshot gets a fresh local vector namespace.
    service.restore(archive)
    assert restored_index_generation(service.main) != generation
    from types import SimpleNamespace
    from interfaces.api.container import AppContainer
    import infrastructure.ai.chromadb_vector_store as vectors
    import application.paths as paths
    monkeypatch.setattr(paths,'get_db_path',lambda: str(service.main))
    monkeypatch.setattr(vectors,'ChromaDBVectorStore',lambda persist_directory: SimpleNamespace(path=Path(persist_directory)))
    vector_settings = SimpleNamespace(enabled=True,use_qdrant=False,persist_directory=str(service.main.parent/'vectors'))
    container = AppContainer(settings=SimpleNamespace(vector_store=vector_settings))
    assert container.get_vector_store().path.name == 'restored-'+restored_index_generation(service.main)
    vector_settings.use_qdrant=True
    assert AppContainer(settings=SimpleNamespace(vector_store=vector_settings)).get_vector_store() is None
    assert service.recover() == {'recovered':False}


def test_restore_into_empty_directory(service,tmp_path):
    archive = service.path(service.create()['id'])
    empty = BackupService(tmp_path/'empty'/'main.sqlite3')
    result = empty.restore(archive)
    assert result['safety_backup_id'] is None
    assert read_pair(empty) == ('original text',1)


def test_damaged_restore_never_replaces_current_pair(service,tmp_path):
    offline(service)
    before = read_pair(service)
    damaged = tmp_path/'bad.zip'
    damaged.write_bytes(b'invalid')
    with pytest.raises(BackupInvalid):
        service.restore(damaged)
    assert read_pair(service) == before
    assert not service.pending.exists()


def test_pair_replace_failure_rolls_back_both_databases(service,monkeypatch):
    archive = service.path(service.create()['id'])
    change_pair(service)
    offline(service)
    original = service._replace_pair
    attempts = []
    def replace(root):
        if not attempts:
            attempts.append(True)
            import shutil
            shutil.copyfile(root/'main.sqlite3',service.main)
            raise OSError('fixture: second replace failed')
        original(root)
    monkeypatch.setattr(service,'_replace_pair',replace)
    with pytest.raises(OSError):
        service.restore(archive)
    assert read_pair(service) == ('newer text',2)
    assert not service.pending.exists()


def test_killed_restore_recovers_old_pair_and_blocks_direct_start(service):
    import subprocess,sys,os
    archive = service.path(service.create()['id'])
    change_pair(service)
    offline(service)
    program = """
import os,shutil,sys
from infrastructure.persistence.backup_service import BackupService
service=BackupService(sys.argv[1])
def crash(root):
    shutil.copyfile(root/'main.sqlite3',service.main)
    os._exit(23)
service._replace_pair=crash
service.restore(sys.argv[2])
"""
    result = subprocess.run([sys.executable,'-c',program,str(service.main),str(archive)],capture_output=True,timeout=30)
    assert result.returncode == 23, result.stderr.decode(errors='replace')
    assert service.pending.exists()
    with pytest.raises(BackupBusy,match='恢复'):
        DatabaseConnection(str(service.main))
    assert read_pair(service) == ('original text',2)
    assert service.recover()['recovered']
    assert read_pair(service) == ('newer text',2)
    assert not service.pending.exists()


def test_restore_quarantines_jobs_and_preserves_unknown_call_reservations(service):
    with closing(sqlite3.connect(service.main)) as conn, conn:
        conn.execute("UPDATE novels SET autopilot_status='running'")
        conn.execute("INSERT INTO xh_aftermath_tasks(id,novel_id,number,content_hash,status,payload,created_at,updated_at) VALUES ('task','book',1,'hash','queued','{}','now','now')")
    journal = OperationJournal(service.operations)
    unknown = journal.reserve('book','llm','unknown',100,50)
    with closing(sqlite3.connect(service.operations)) as conn, conn:
        conn.execute("INSERT INTO jobs VALUES ('job','book',1,'running',?, '{}')",(json.dumps({'status':'running'}),))
    archive = service.path(service.create()['id'])
    offline(service)
    service.restore(archive)
    with closing(sqlite3.connect(service.main)) as conn, conn:
        assert conn.execute('SELECT status FROM xh_aftermath_tasks').fetchone()[0] == 'failed'
        assert conn.execute('SELECT autopilot_status FROM novels').fetchone()[0] == 'stopped'
    with closing(sqlite3.connect(service.operations)) as conn, conn:
        status,body = conn.execute('SELECT status,body FROM jobs').fetchone()
        assert status == json.loads(body)['status'] == 'interrupted'
        assert conn.execute('SELECT input_tokens,reserved_tokens FROM calls WHERE id=?',(unknown,)).fetchone() == (None,150)


def test_archive_version_validation_does_not_modify_target(service,tmp_path):
    original = service.path(service.create()['id'])
    with zipfile.ZipFile(original) as archive:
        entries = {name:archive.read(name) for name in archive.namelist()}
    manifest = json.loads(entries['manifest.json'])
    manifest['release']='999.0.0'
    entries['manifest.json']=json.dumps(manifest).encode()
    future = tmp_path/'future.zip'
    with zipfile.ZipFile(future,'w') as archive:
        for name,value in entries.items(): archive.writestr(name,value)
    offline(service)
    with pytest.raises(BackupInvalid,match='版本'):
        service.restore(future)
    assert read_pair(service) == ('original text',1)
    validate_version('0.6.0')
    manifest['release']='0.6.0'
    entries['manifest.json']=json.dumps(manifest).encode()
    with zipfile.ZipFile(future,'w') as archive:
        for name,value in entries.items(): archive.writestr(name,value)
    assert service.restore(future)['restored']
    assert read_pair(service) == ('original text',1)


def test_runtime_leases_allow_multiple_readers_but_refuse_restore(tmp_path):
    a=file_lock(tmp_path/'runtime',shared=True)
    b=file_lock(tmp_path/'runtime',shared=True)
    try:
        with pytest.raises(BackupBusy): file_lock(tmp_path/'runtime')
    finally:
        a.close();b.close()
    file_lock(tmp_path/'runtime').close()


def test_backup_blocks_writers_to_both_files_at_snapshot_boundary(service,monkeypatch):
    import infrastructure.persistence.backup_service as module
    real_connect = sqlite3.connect
    probes = []
    def connect(path,*args,**kwargs):
        # The read-only backup readers are opened only after acquiring both write locks.
        if kwargs.get('uri') and str(path).split('?')[0] in {service.main.as_uri(),service.operations.as_uri()}:
            for target in (service.main,service.operations):
                with closing(real_connect(target,timeout=.01)) as writer:
                    with pytest.raises(sqlite3.OperationalError,match='locked'):
                        writer.execute('BEGIN IMMEDIATE')
                    probes.append(target)
        return real_connect(path,*args,**kwargs)
    monkeypatch.setattr(module.sqlite3,'connect',connect)
    service.create()
    assert len(probes) == 4


def test_backup_http_roundtrip_and_upload_validation(service):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from interfaces.api.v1.core import backup_routes as routes
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_backup_service] = lambda: service
    with TestClient(app) as client:
        created=client.post('/maintenance/backups')
        assert created.status_code == 200
        id=created.json()['id']
        assert client.get('/maintenance/backups').json()[0]['id'] == id
        exported=client.get('/maintenance/backups/'+id+'/export')
        assert exported.status_code == 200
        assert exported.headers['cache-control'] == 'no-store'
        assert client.post('/maintenance/backups/'+id+'/verify').status_code == 200
        result=client.post('/maintenance/backups/inspect',files={'file':('backup.zip',exported.content,'application/zip')})
        assert result.status_code == 200
        assert result.json()['counts']['main']['chapters'] == 1
        assert client.post('/maintenance/backups/inspect',files={'file':('bad.zip',b'bad','application/zip')}).status_code == 422
        assert client.get('/maintenance/backups/nope/export').status_code == 422
        assert 'original text' not in client.get('/maintenance/backups').text


def test_paired_wal_snapshot_contains_both_databases_and_verifies(service):
    created = service.create()
    assert created['counts']['main']['chapters'] == 1
    assert created['counts']['operations']['calls'] == 1
    checked = service.inspect(service.path(created['id']))
    assert checked['id'] == created['id']
    assert service.list()[0]['valid_manifest']
    with zipfile.ZipFile(service.path(created['id'])) as archive:
        assert set(archive.namelist()) == {'main.sqlite3','operations.sqlite3','manifest.json'}


@pytest.mark.parametrize('version', ['0.5.0','999.0.0','invalid'])
def test_incompatible_versions_are_rejected(version):
    with pytest.raises(BackupInvalid):
        validate_version(version)


def test_bad_zip_and_path_escape_are_rejected(service, tmp_path):
    damaged = tmp_path/'bad.zip'
    damaged.write_bytes(b'not a zip')
    with pytest.raises(BackupInvalid):
        service.inspect(damaged)
    with zipfile.ZipFile(damaged,'w') as archive:
        archive.writestr('../main.sqlite3', b'bad')
    with pytest.raises(BackupInvalid):
        service.inspect(damaged)
    with pytest.raises(BackupInvalid):
        service.path('../outside')


def test_tampered_database_is_rejected(service, tmp_path):
    created = service.create()
    with zipfile.ZipFile(service.path(created['id'])) as archive:
        entries = {name:archive.read(name) for name in archive.namelist()}
    entries['main.sqlite3'] += b'tampered'
    changed = tmp_path/'tampered.zip'
    with zipfile.ZipFile(changed,'w') as archive:
        for name,data in entries.items():
            archive.writestr(name,data)
    with pytest.raises(BackupInvalid, match='校验'):
        service.inspect(changed)
