"""Package the browser source and compiled UI, excluding all local user data."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = {".github", "application", "audit", "config", "domain", "engine", "frontend",
               "infrastructure", "interfaces", "project_docs", "scripts", "shared", "tests"}
SOURCE_FILES = {".env.example", ".gitignore", ".gitattributes", "AGENTS.md", "LICENSE", "NOTICE.md", "CONTRIBUTORS.md", "README.md",
                "README_LOGGING.md", "VERSION", "pyproject.toml", "pytest.ini", "load_env.py",
                "cli.py", "__init__.py", "__main__.py", "requirements.txt",
                "requirements-tested.txt", "requirements-local.txt", "start-xuheng.ps1", "start-xuheng.cmd"}
EXCLUDED_PARTS = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", "data",
                  "logs", "test-results", "playwright-report", ".validation", ".release"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".db", ".sqlite", ".sqlite3", ".log", ".pem", ".key",
                     ".exe", ".dll", ".zip"}


def allowed(name: str) -> bool:
    path = PurePosixPath(name)
    return (not path.is_absolute() and ".." not in path.parts
            and (path.parts[0] in SOURCE_DIRS or name in SOURCE_FILES)
            and not EXCLUDED_PARTS.intersection(path.parts)
            and path.suffix.lower() not in EXCLUDED_SUFFIXES
            and (not path.name.startswith(".env") or name == ".env.example"))


def main():
    dist = ROOT / "frontend/dist"
    if not (dist / "index.html").is_file():
        raise SystemExit("Build frontend first: cd frontend; npm ci; npm run build")
    listed = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                            cwd=ROOT, check=True, capture_output=True).stdout.decode("utf-8").split("\0")
    files = {name: ROOT / name for name in listed if name and allowed(name) and (ROOT / name).is_file()}
    for path in dist.rglob("*"):
        name = path.relative_to(ROOT).as_posix()
        if path.is_file() and allowed(name):
            files[name] = path
    required = {"LICENSE", "CONTRIBUTORS.md", "frontend/dist/index.html", "project_docs/GETTING_STARTED.md",
                "scripts/evaluation/baseline_conflict_service.txt"}
    if not required <= files.keys():
        raise SystemExit(f"Missing release files: {sorted(required - files.keys())}")
    output = ROOT / ".release"
    output.mkdir(exist_ok=True)
    version = json.loads((ROOT / "frontend/package.json").read_text(encoding="utf-8"))["version"]
    target = output / f"Xuheng-browser-{version}.zip"
    manifest = {"product": "Xuheng browser", "version": version, "edition": "source + compiled frontend",
                "upstream": "https://github.com/shenminglinyi/PlotPilot",
                "baseline": "8dba88fad06368a632522ce6f98e17f9cfc7db2a", "files": {}}
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, path in sorted(files.items()):
            content = path.read_bytes()
            manifest["files"][name] = hashlib.sha256(content).hexdigest()
            archive.writestr(f"Xuheng/{name}", content)
        archive.writestr("Xuheng/RELEASE-MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None
        assert all(allowed(n.removeprefix("Xuheng/")) or n == "Xuheng/RELEASE-MANIFEST.json" for n in archive.namelist())
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(".zip.sha256").write_text(f"{digest}  {target.name}\n", encoding="ascii")
    print(json.dumps({"archive": str(target), "files": len(files), "bytes": target.stat().st_size, "sha256": digest}))


if __name__ == "__main__":
    main()
