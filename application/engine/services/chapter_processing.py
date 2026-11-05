"""Durable post-save boundaries. Reuse only a matching book/chapter/text version."""
from hashlib import sha256
from pathlib import Path


def content_digest(content):
    return sha256((content or "").encode("utf-8")).hexdigest()


def journal_for_db(db):
    path = getattr(db,"db_path",None)
    if not isinstance(path,str):
        return None
    from infrastructure.persistence.operation_journal import journal_at
    return journal_at(str(Path(path).with_suffix(".operations.sqlite3")))


def journal_for_host(host):
    for name in ("novel_repository","chapter_repository"):
        journal = journal_for_db(getattr(getattr(host,name,None),"db",None))
        if journal is not None:
            return journal
    return None


def journal_for_context(ctx):
    journal = journal_for_db(getattr(getattr(ctx,"chapter_repository",None),"db",None))
    if journal is None:
        journal = journal_for_host(ctx.get_dep("autopilot_host"))
    return journal


def pending_saved_chapter(db, novel_id):
    """A saved text with an unfinished checkpoint must finish audit before writing.

    Legacy chapters without a checkpoint keep the existing recovery behavior.
    'saving' is included to cover a crash between the manuscript commit and the
    journal acknowledgement. Hashes must match the actual saved chapter.
    """
    journal = journal_for_db(db)
    if journal is None:
        return None
    rows = journal.chapter_progress(novel_id,pending_only=True)
    for row in sorted(rows,key=lambda item:item["number"]):
        if row["status"] == "completed":
            continue
        chapter = db.fetch_one("SELECT content,status FROM chapters WHERE novel_id=? AND number=?",(novel_id,row["number"]))
        if chapter and chapter["status"] == "completed" and chapter["content"] and content_digest(chapter["content"]) == row["content_hash"]:
            return row["number"]
    return None


def completed_count(host, novel_id):
    db = getattr(getattr(host,"chapter_repository",None),"db",None)
    if not isinstance(getattr(db,"db_path",None),str):
        return None
    row = db.fetch_one("SELECT count(*) AS n FROM chapters WHERE novel_id=? AND status='completed' AND trim(coalesce(content,''))<>''",(novel_id,))
    return int(row["n"])


def audit_completed(db, novel_id, number):
    journal=journal_for_db(db)
    if journal is None or number is None:
        return False
    chapter=db.fetch_one("SELECT content,status FROM chapters WHERE novel_id=? AND number=?",(novel_id,number))
    if not chapter or chapter["status"]!="completed":
        return False
    row=journal.chapter_checkpoint(novel_id,number,content_digest(chapter["content"]))
    return row is not None and row["status"]=="completed"
