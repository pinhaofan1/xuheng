"""Verified paired backup CLI. Restore is a preview unless --apply is explicit."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from infrastructure.persistence.backup_service import BackupService, BackupInvalid, BackupBusy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['create','list','inspect','restore','recover'])
    parser.add_argument('archive', nargs='?')
    parser.add_argument('--database')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    from application.paths import get_db_path
    service = BackupService(args.database or get_db_path())
    try:
        if args.action in ('restore','inspect'):
            if not args.archive:
                parser.error('archive is required')
            result = service.restore(args.archive) if args.action == 'restore' and args.apply else service.inspect(args.archive)
        else:
            result = getattr(service,args.action)()
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except (BackupInvalid, BackupBusy, OSError) as exc:
        print(str(exc),file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
