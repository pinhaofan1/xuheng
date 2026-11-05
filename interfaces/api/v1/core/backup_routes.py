"""Global backups. Live HTTP validates archives; replacement is offline only."""
from pathlib import Path
import tempfile

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse
from infrastructure.persistence.backup_service import BackupService, BackupInvalid, BackupBusy, MAX_FILE_BYTES
from infrastructure.persistence.database.connection import get_database
from infrastructure.persistence.operation_journal import get_operation_journal

router = APIRouter(prefix='/maintenance/backups', tags=['backups'])


def get_backup_service():
    get_operation_journal()
    return BackupService(get_database().db_path)


def checked(operation):
    try:
        return operation()
    except BackupBusy as exc:
        raise HTTPException(409,str(exc)) from exc
    except BackupInvalid as exc:
        raise HTTPException(422,str(exc)) from exc
    except OSError as exc:
        raise HTTPException(503,'备份文件暂时无法访问，请检查数据目录空间与权限') from exc


@router.get('')
def list_backups(service: BackupService = Depends(get_backup_service)):
    return checked(service.list)


@router.post('')
def create_backup(service: BackupService = Depends(get_backup_service)):
    return checked(service.create)


@router.post('/inspect')
def inspect_upload(file: UploadFile, service: BackupService = Depends(get_backup_service)):
    def inspect():
        with tempfile.TemporaryDirectory(prefix='.upload-',dir=service.main.parent) as temporary:
            target = Path(temporary)/'archive.zip'
            size = 0
            with target.open('wb') as writer:
                for chunk in iter(lambda: file.file.read(1024*1024),b''):
                    size += len(chunk)
                    if size > MAX_FILE_BYTES*2 + 131072:
                        raise BackupInvalid('上传备份超过大小限制')
                    writer.write(chunk)
            return service.inspect(target)
    try:
        return checked(inspect)
    finally:
        file.file.close()


@router.post('/{backup_id}/verify')
def verify_backup(backup_id: str, service: BackupService = Depends(get_backup_service)):
    return checked(lambda: service.inspect(service.path(backup_id)))


@router.get('/{backup_id}/export')
def download_backup(backup_id: str, service: BackupService = Depends(get_backup_service)):
    path = checked(lambda: service.path(backup_id))
    if not path.is_file():
        raise HTTPException(404,'备份不存在')
    return FileResponse(path,media_type='application/zip',filename=path.name,headers={'Cache-Control':'no-store'})
