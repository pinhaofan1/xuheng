from unittest.mock import Mock

from application.world.services.cast_service import CastService
from application.world.dtos.cast_dto import CastGraphDTO, CharacterDTO
from infrastructure.persistence.database.connection import DatabaseConnection


def test_coverage_reads_latest_saved_content_and_never_resurrects_deleted_files(tmp_path):
    db=DatabaseConnection(str(tmp_path/"book.db"))
    # The shared unit-test schema intentionally omits optional Bible tables.
    db.execute("CREATE TABLE IF NOT EXISTS bible_characters(id TEXT PRIMARY KEY,novel_id TEXT,name TEXT)")
    for book in ("book","other"):
        db.execute("INSERT INTO novels(id,title,slug) VALUES (?,?,?)",(book,book,book))
    db.execute("INSERT INTO chapters(id,novel_id,number,title,content) VALUES ('c','book',1,'正文','阿澜和「沈舟」在桥边。')")
    db.execute("INSERT INTO chapters(id,novel_id,number,title,content) VALUES ('empty','book',2,'未写','')")
    db.execute("INSERT INTO chapters(id,novel_id,number,title,content) VALUES ('foreign','other',3,'外书','沈澜')")
    db.execute("INSERT INTO bible_characters(id,novel_id,name) VALUES ('shen','book','沈舟')")
    db.commit()
    folder=tmp_path/"novels"/"book";folder.mkdir(parents=True)
    (folder/"chapter_99.md").write_text("沈澜，过时导出正文",encoding="utf-8")
    service=CastService(tmp_path,db=db)
    service.get_cast_graph=Mock(return_value=CastGraphDTO(version=2,characters=[CharacterDTO(id="p",name="沈澜",aliases=["阿澜"],role="",traits="",note="",story_events=[])],relationships=[]))
    result=service.get_cast_coverage("book")
    assert result.chapters_scanned==1 and result.content_source=="database"
    assert result.characters[0].chapter_ids==[1]
    assert result.bible_not_in_cast[0].chapter_ids==[1]
    assert result.quoted_not_in_cast[0].text=="沈舟"
    db.execute("UPDATE chapters SET content='没人出场' WHERE id='c'");db.commit()
    assert not service.get_cast_coverage("book").characters[0].mentioned
    db.execute("DELETE FROM chapters WHERE id='c'");db.commit()
    assert service.get_cast_coverage("book").chapters_scanned==0
    assert service.get_cast_coverage("book").characters[0].chapter_ids==[]
    db.close_all()
