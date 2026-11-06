"""Verify an unpacked browser package using a disposable, credential-free workspace."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from urllib.request import Request, urlopen
from uuid import uuid4
import zipfile

from package_xuheng import ROOT, allowed


def unpack(archive_path, destination):
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        manifest_name = 'Xuheng/RELEASE-MANIFEST.json'
        manifest = json.loads(archive.read(manifest_name))
        expected = {'Xuheng/' + name for name in manifest['files']}
        assert len(names) == len(set(names)) and set(names) == expected | {manifest_name}
        for name, digest in manifest['files'].items():
            assert allowed(name), name
            payload = archive.read('Xuheng/' + name)
            assert hashlib.sha256(payload).hexdigest() == digest, name
            target = destination / 'Xuheng' / name
            assert target.resolve().is_relative_to(destination / 'Xuheng'), name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
    return manifest


def smoke(archive_path, scratch):
    manifest = unpack(archive_path, scratch)
    project = scratch / 'Xuheng'
    data = project / 'data'
    version = json.loads((project/'frontend/package.json').read_text(encoding='utf-8'))['version']
    assert version == manifest['version']
    assert version == json.loads((project/'project_docs/DEVELOPMENT_PROGRESS.json').read_text(encoding='utf-8'))['version']
    env = {name: value for name, value in os.environ.items()
           if not name.endswith(('_API_KEY', '_AUTH_TOKEN')) and name not in ('PYTHONPATH', 'XUHENG_VALIDATION_BUDGET_REQUIRED')}
    env.update(DISABLE_AUTO_DAEMON='1', EMBEDDING_SERVICE='openai', PYTHONIOENCODING='utf-8',
               PLOTPILOT_PROD_DATA_DIR=str(data), GIT_CEILING_DIRECTORIES=str(scratch),
               LOG_FILE=str(scratch/'app.log'), XUHENG_SMOKE_ID=uuid4().hex)

    def script(*args):
        result = subprocess.run([sys.executable, *args], cwd=project, env=env, capture_output=True, text=True,
                                encoding='utf-8', timeout=45, check=True)
        return result.stdout

    script('scripts/evaluation/evaluate_xuheng.py', '--output', str(scratch/'offline.json'))
    script('scripts/seed_xuheng_demo.py')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    launcher = ('import os,sys,uvicorn; from interfaces.main import app; '
                'from scripts.validation.server import register_identity; '
                'register_identity(app, os.environ["XUHENG_SMOKE_ID"]); '
                'uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]))')

    def request(path, *, method='GET', payload=None):
        headers = {'Content-Type':'application/json'} if payload is not None else {}
        with urlopen(Request(f'http://127.0.0.1:{port}'+path, data=payload, method=method, headers=headers), timeout=12) as response:
            return response.read()

    with (scratch/'server.log').open('wb') as log:
        process = subprocess.Popen([sys.executable, '-c', launcher, str(port)], cwd=project, env=env,
                                   stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        owned = False
        try:
            deadline = monotonic()+40
            while monotonic()<deadline and process.poll() is None:
                try:
                    identity = json.loads(request('/internal/validation-status'))
                    owned = identity.get('run_id') == env['XUHENG_SMOKE_ID']
                    if owned:
                        break
                except (OSError, ValueError):
                    pass
                sleep(.2)
            assert owned, 'Isolated package API did not become ready'
            assert not json.loads(request('/api/v1/evidence-reviews/model/status'))['configured']
            review = json.loads(request('/api/v1/evidence-reviews/xuheng-demo/chapters/2', method='POST', payload=b'{}'))
            assert len(review['issues']) == 2 and len(review['uncertain']) == 1
            page = request('/book/xuheng-demo/review/2')
            assert b'<title>' in page and b'/assets/' in page
        finally:
            if owned and process.poll() is None:
                try:
                    request('/internal/shutdown', method='POST', payload=b'{}')
                except OSError:
                    pass
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=8)

    for name in ('restore-xuheng.cmd', 'restore-xuheng.ps1', 'scripts/manage_backups.py'):
        assert (project/name).is_file(), name
    backup = json.loads(script('scripts/manage_backups.py', 'create'))
    archive = data/'backups'/('xuheng-'+backup['id']+'.zip')
    assert json.loads(script('scripts/manage_backups.py', 'restore', str(archive), '--apply'))['restored']
    print(json.dumps({'version':version,'archive_startup':'passed','offline_review':'passed',
                      'model_configured':False,'paired_restore':'passed'}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    args = parser.parse_args()
    with TemporaryDirectory(prefix='xuheng-package-') as directory:
        smoke(args.archive.resolve(), Path(directory).resolve())
