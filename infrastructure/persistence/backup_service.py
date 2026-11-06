"""Paired SQLite snapshots and verified, offline restore with a rollback journal."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import time
from uuid import uuid4
import zipfile

FILES = {"main": "main.sqlite3", "operations": "operations.sqlite3"}
MAX_FILE_BYTES = 256 * 1024 * 1024
BACKUP_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class BackupInvalid(ValueError):
    pass


class BackupBusy(ValueError):
    pass


def release_version():
    return json.loads((Path(__file__).resolve().parents[2] / 'frontend/package.json').read_text(encoding='utf-8'))['version']


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def check_database(path, required):
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True)) as conn:
            conn.execute('PRAGMA trusted_schema=OFF')
            if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise BackupInvalid('数据库完整性检查失败')
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not set(required) <= tables:
                raise BackupInvalid('备份缺少必要的数据表')
            return {table: conn.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in required}
    except sqlite3.DatabaseError as exc:
        raise BackupInvalid('备份数据库无法读取或已经损坏') from exc


def validate_version(value, current=None):
    def version(text):
        if not isinstance(text, str) or not re.fullmatch(r'\d+\.\d+\.\d+', text):
            raise BackupInvalid('备份版本标记无效')
        return tuple(int(n) for n in text.split('.'))
    source, target = version(value), version(current or release_version())
    if source < (0,6,0) or source > target:
        raise BackupInvalid('此备份版本不在当前应用支持的范围内，请使用匹配或更新版本恢复')


@contextmanager
def maintenance_lock(directory):
    """OS-released lock: a killed backup/restore cannot permanently lock startup."""
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    handle = file_lock(directory / '.xuheng-maintenance.lock')
    try:
        yield
    finally:
        handle.close()


def file_lock(path, shared=False):
    """Process lifetime lease; the OS releases it even after a hard crash."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open('a+b')
    try:
        # Locking beyond EOF is supported; no write to another process's lock.
        handle.seek(0)
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            import msvcrt
            class Overlapped(ctypes.Structure):
                _fields_ = [('Internal',ctypes.c_size_t),('InternalHigh',ctypes.c_size_t),
                            ('Offset',wintypes.DWORD),('OffsetHigh',wintypes.DWORD),('hEvent',wintypes.HANDLE)]
            kernel = ctypes.WinDLL('kernel32',use_last_error=True)
            lock = kernel.LockFileEx
            lock.argtypes = [wintypes.HANDLE,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,wintypes.DWORD,ctypes.POINTER(Overlapped)]
            lock.restype = wintypes.BOOL
            if not lock(msvcrt.get_osfhandle(handle.fileno()),1 if shared else 3,0,1,0,ctypes.byref(Overlapped())):
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            import fcntl
            fcntl.flock(handle.fileno(), (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        return handle
    except OSError as exc:
        handle.close()
        raise BackupBusy('数据仍被应用、守护进程或另一项恢复操作使用，请先关闭后重试') from exc


class BackupService:
    def __init__(self, db_path):
        self.main = Path(db_path).resolve()
        self.operations = self.main.with_suffix('.operations.sqlite3')
        self.directory = self.main.parent / 'backups'
        self.pending = self.main.parent / '.xuheng-restore.json'

    def path(self, id):
        if not BACKUP_PATTERN.fullmatch(id):
            raise BackupInvalid('备份编号无效')
        path = self.directory / f'xuheng-{id}.zip'
        if path.resolve().parent != self.directory.resolve():
            raise BackupInvalid('备份路径无效')
        return path

    def create(self):
        with maintenance_lock(self.main.parent):
            return self._create()

    def _create(self):
        if self.pending.exists():
            raise BackupBusy('检测到未完成的恢复，请先完成恢复处理')
        if not self.main.is_file() or not self.operations.is_file():
            raise BackupInvalid('主库或运行账本缺失，无法创建联合备份')
        self.directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.snapshot-', dir=self.directory) as temporary:
            root = Path(temporary).resolve()
            deadline = time.monotonic()+30
            def progress(status, remaining, total):
                if time.monotonic() > deadline:
                    raise BackupBusy('数据库快照超时，请在任务较少时重试')
            # Freeze writes in both files at the same boundary, including writers
            # in the daemon process. Backup readers include committed WAL pages.
            try:
                with closing(sqlite3.connect(self.main, timeout=5)) as gate:
                    gate.execute('ATTACH DATABASE ? AS operations', (str(self.operations),))
                    gate.execute('BEGIN IMMEDIATE')
                    try:
                        for role, source in [('main', self.main), ('operations', self.operations)]:
                            with closing(sqlite3.connect(source.as_uri()+'?mode=ro', uri=True)) as reader:
                                reader.execute('BEGIN')
                                reader.execute('SELECT count(*) FROM sqlite_master').fetchone()
                                with closing(sqlite3.connect(root / FILES[role])) as target:
                                    reader.backup(target, pages=256, progress=progress, sleep=.01)
                    finally:
                        gate.rollback()
            except sqlite3.OperationalError as exc:
                raise BackupBusy('数据库正在写入，联合备份未完成，请稍后重试') from exc
            counts = {'main': check_database(root/FILES['main'], ['novels','chapters']),
                      'operations': check_database(root/FILES['operations'], ['calls','policies'])}
            if any((root/name).stat().st_size > MAX_FILE_BYTES for name in FILES.values()):
                raise BackupInvalid('数据库超出当前联合备份的单库 256 MB 限制')
            id = uuid4().hex
            manifest = {'format': 'xuheng-paired-backup', 'format_version': 1, 'id': id,
                        'created_at': utcnow(), 'release': release_version(), 'counts': counts,
                        'files': {role: {'name': name, 'sha256': sha(root/name), 'bytes': (root/name).stat().st_size}
                                  for role,name in FILES.items()}}
            temporary_zip = root / 'archive.zip'
            with zipfile.ZipFile(temporary_zip, 'w', zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
                for name in FILES.values():
                    archive.write(root/name, name)
            os.replace(temporary_zip, self.path(id))
            return {**manifest, 'bytes': self.path(id).stat().st_size}

    def list(self):
        rows = []
        if not self.directory.is_dir():
            return rows
        for path in sorted(self.directory.glob('xuheng-*.zip'), key=lambda p:p.stat().st_mtime, reverse=True)[:100]:
            id = path.stem.removeprefix('xuheng-')
            try:
                if self.path(id).resolve() != path.resolve():
                    continue
                with zipfile.ZipFile(path) as archive:
                    info = archive.getinfo('manifest.json')
                    if info.file_size > 65536:
                        raise BackupInvalid('备份信息超出限制')
                    manifest = json.loads(archive.read(info))
                rows.append({'id':id,'created_at':manifest['created_at'],'release':manifest['release'],
                             'counts':manifest['counts'],'bytes':path.stat().st_size,'valid_manifest':True})
            except (ValueError, KeyError, TypeError, OSError, zipfile.BadZipFile):
                rows.append({'id':id,'valid_manifest':False,'bytes':path.stat().st_size})
        return rows

    def inspect(self, source):
        self.main.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.verify-', dir=self.main.parent) as temporary:
            return self.extract_verified(source, Path(temporary))

    def restore(self, source):
        """Offline only. Never replace databases while an application holds a lease."""
        with maintenance_lock(self.main.parent), closing(file_lock(self.main.parent/'.xuheng-runtime.lock')):
            if self.pending.exists():
                raise BackupBusy('存在未完成的恢复，请先运行 recover')
            with tempfile.TemporaryDirectory(prefix='.restore-check-', dir=self.main.parent) as temporary:
                root = Path(temporary)
                manifest = self.extract_verified(source, root)  # no changes before full validation
                existing = self.main.exists() or self.operations.exists()
                if existing and not (self.main.is_file() and self.operations.is_file()):
                    raise BackupInvalid('现有数据缺少主库或账本，请先修复后恢复')
                safety = self._create()['id'] if existing else None
                self._checkpoint()
                for role in FILES:
                    quarantine(root/FILES[role], role)
                stage_id = uuid4().hex
                stage = self.main.parent / ('.restore-'+stage_id)
                stage.mkdir()
                for name in FILES.values():
                    shutil.copyfile(root/name, stage/name)
                journal = {'format':1, 'database':self.main.name, 'stage':stage_id, 'safety':safety}
                write_json(self.pending, journal)
                try:
                    self._replace_pair(stage)
                    self._verify_pair()
                except BaseException:
                    self._recover_locked()
                    raise
                self.pending.unlink()
                self._cleanup_stage(stage_id)
                return {'restored':True, 'backup':manifest, 'safety_backup_id':safety,
                        'message':'恢复完成；托管和未完成任务已停止，请核对正文与账目后手动继续。'}

    def recover(self):
        if not self.pending.exists():
            return {'recovered':False}
        with maintenance_lock(self.main.parent), closing(file_lock(self.main.parent/'.xuheng-runtime.lock')):
            return self._recover_locked()

    def _recover_locked(self):
        try:
            journal = json.loads(self.pending.read_text(encoding='utf-8'))
            if journal['format'] != 1 or journal['database'] != self.main.name or not BACKUP_PATTERN.fullmatch(journal['stage']):
                raise BackupInvalid('恢复日志无效，请保留数据目录并联系维护者')
            self._checkpoint()
            if journal['safety']:
                with tempfile.TemporaryDirectory(prefix='.rollback-', dir=self.main.parent) as temporary:
                    root = Path(temporary)
                    self.extract_verified(self.path(journal['safety']), root)
                    for role in FILES:
                        quarantine(root/FILES[role], role)
                    self._replace_pair(root)
                    self._verify_pair()
            else:
                for path in (self.main,self.operations):
                    path.unlink(missing_ok=True)
            self.pending.unlink()
            self._cleanup_stage(journal['stage'])
            return {'recovered':True,'safety_backup_id':journal['safety']}
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise BackupInvalid('恢复回滚未完成，已保留安全副本和恢复日志；请勿启动应用') from exc

    def _cleanup_stage(self, id):
        if not BACKUP_PATTERN.fullmatch(id):
            raise BackupInvalid('恢复目录编号无效')
        stage = self.main.parent/('.restore-'+id)
        if stage.is_symlink() or stage.resolve().parent != self.main.parent.resolve():
            raise BackupInvalid('恢复目录路径无效')
        # Only the two exact files created by this service; never recursively delete.
        for name in FILES.values():
            (stage/name).unlink(missing_ok=True)
        if stage.exists():
            stage.rmdir()

    def _checkpoint(self):
        for path in (self.main,self.operations):
            if path.exists():
                with closing(sqlite3.connect(path, timeout=1)) as conn:
                    if conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]:
                        raise BackupBusy('数据库仍被其他进程使用，请关闭后再恢复')
                    conn.execute('BEGIN EXCLUSIVE')
                    conn.rollback()
            for suffix in ('-wal','-shm'):
                sidecar = Path(str(path)+suffix)
                if sidecar.exists():
                    sidecar.unlink()

    def _replace_pair(self, root):
        for role,target in [('main',self.main),('operations',self.operations)]:
            temporary = target.with_name(target.name+'.restore-tmp')
            shutil.copyfile(root/FILES[role], temporary)
            with temporary.open('r+b') as stream:
                os.fsync(stream.fileno())
            os.replace(temporary,target)

    def _verify_pair(self):
        check_database(self.main,['novels','chapters'])
        check_database(self.operations,['calls','policies'])

    @staticmethod
    def extract_verified(source, root):
        try:
            with zipfile.ZipFile(source) as archive:
                infos = archive.infolist()
                if len(infos) != 3 or {i.filename for i in infos} != {'manifest.json', *FILES.values()}:
                    raise BackupInvalid('备份内容不完整或含有不支持的文件')
                for info in infos:
                    if info.file_size > (65536 if info.filename == 'manifest.json' else MAX_FILE_BYTES) or info.flag_bits & 1 or (info.external_attr >> 16) & 0o170000 == 0o120000:
                        raise BackupInvalid('备份文件过大、被加密或包含不支持的文件类型')
                manifest = json.loads(archive.read('manifest.json'))
                if not isinstance(manifest,dict):
                    raise BackupInvalid('备份信息格式无效')
                if manifest.get('format') != 'xuheng-paired-backup' or manifest.get('format_version') != 1:
                    raise BackupInvalid('不支持此备份格式')
                validate_version(manifest.get('release'))
                if not isinstance(manifest.get('id'), str) or not BACKUP_PATTERN.fullmatch(manifest['id']):
                    raise BackupInvalid('备份编号无效')
                for role,name in FILES.items():
                    expected = manifest['files'][role]
                    if expected['name'] != name:
                        raise BackupInvalid('备份文件标记不匹配')
                    target = root/name
                    with archive.open(name) as reader, target.open('wb') as writer:
                        shutil.copyfileobj(reader, writer, 1024*1024)
                    if target.stat().st_size != expected['bytes'] or sha(target) != expected['sha256']:
                        raise BackupInvalid('备份校验值不匹配，文件可能被修改或损坏')
                counts = {'main': check_database(root/FILES['main'], ['novels','chapters']),
                          'operations': check_database(root/FILES['operations'], ['calls','policies'])}
                if counts != manifest['counts']:
                    raise BackupInvalid('备份统计与数据库内容不一致')
                return manifest
        except BackupInvalid:
            raise
        except (ValueError, KeyError, TypeError, OSError, zipfile.BadZipFile, RuntimeError) as exc:
            raise BackupInvalid('备份无法读取或已经损坏') from exc


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary,path)


def quarantine(path, role):
    """Restoring historical state must never automatically spend on model calls."""
    with closing(sqlite3.connect(path)) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if role == 'main':
            conn.execute('CREATE TABLE IF NOT EXISTS xh_restore_state(id INTEGER PRIMARY KEY CHECK(id=1), generation TEXT NOT NULL)')
            conn.execute('INSERT OR REPLACE INTO xh_restore_state VALUES (1,?)',(uuid4().hex,))
            conn.execute("UPDATE novels SET autopilot_status='stopped'")
            if 'xh_search_jobs' in tables:
                for table in ('xh_search_postings','xh_search_chunks','xh_search_documents'):
                    conn.execute(f'DELETE FROM {table}')
                conn.execute("UPDATE xh_search_jobs SET status='cancelled',error='从备份恢复，请重建索引'")
            if 'xh_search_vector_jobs' in tables:
                conn.execute('DELETE FROM xh_search_vectors')
                conn.execute("""UPDATE xh_search_vector_jobs SET id=lower(hex(randomblob(16))),status='cancelled',
                    error='从备份恢复，请核对服务用量并重新构建向量索引'""")
            if 'xh_aftermath_tasks' in tables:
                conn.execute("UPDATE xh_aftermath_tasks SET status='failed',owner=NULL,heartbeat=0,error=? WHERE status IN ('queued','running')",
                             ('从备份恢复：请核对已保存结果与模型账单后手动继续',))
            for table in ('ai_invocation_sessions','ai_invocation_attempts'):
                if table in tables:
                    conn.execute("UPDATE "+table+" SET status='failed' WHERE status IN ('generating','running','pending')")
        elif 'jobs' in tables:
            for id,body in conn.execute("SELECT id,body FROM jobs WHERE status IN ('queued','running')").fetchall():
                job = json.loads(body)
                job.update(status='interrupted', error='从备份恢复：请核对后手动重新发起', updated_at=utcnow())
                conn.execute("UPDATE jobs SET status='interrupted',body=? WHERE id=?",(json.dumps(job,ensure_ascii=False),id))
        conn.commit()
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')


def restored_index_generation(db_path):
    """Old external vectors must not be paired with restored historical text."""
    path = Path(db_path).resolve()
    if not path.is_file():
        return None
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='xh_restore_state' AND type='table'").fetchone():
            return None
        row = conn.execute('SELECT generation FROM xh_restore_state WHERE id=1').fetchone()
        if not row or not BACKUP_PATTERN.fullmatch(row[0]):
            raise BackupInvalid('恢复后的索引版本标记无效')
        return row[0]
