import json
import sqlite3
from pathlib import Path

import pytest

from application.world.services.cast_service import CastService
from application.world.services.cast_runtime_settings import CastRuntimeSettings
from domain.bible.triple import Triple
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.entity_display_labels import EntityDisplayLabels
from infrastructure.persistence.database.sqlite_knowledge_repository import SqliteKnowledgeRepository
from infrastructure.persistence.database.triple_repository import TripleRepository


@pytest.fixture
def graph_db(tmp_path):
    db = DatabaseConnection(str(tmp_path / 'graph.db'))
    conn = sqlite3.connect(db.db_path)
    for book in ('n1', 'n2'):
        conn.execute("INSERT INTO novels(id,title,slug,target_chapters) VALUES(?,?,?,10)", (book, book, book))
        conn.execute("INSERT INTO story_nodes(id,novel_id,node_type,number,title,order_index) VALUES(?,?,'chapter',4,'出场',4)", (book+'-ch4', book))
    for actor, book, name in [('novel-n1-char-1', 'n1', '林风'), ('novel-n1-char-2', 'n1', '秦柔'), ('novel-n2-char-1', 'n2', '另一书人物')]:
        conn.execute('INSERT INTO unified_characters(id,novel_id,name) VALUES(?,?,?)', (actor,book,name))
    conn.execute("INSERT INTO bible_locations(id,novel_id,name) VALUES('heishi_fang','n1','黑市坊')")
    for book, name in [('n1','药铺掌柜'),('n2','另一位掌柜')]:
        conn.execute("""INSERT INTO chapter_elements(id,chapter_id,element_type,element_id,relation_type,importance,notes)
            VALUES(?,?,'character','ephemeral-4-deadbeef','appears','minor',?)""",
            (book+'-slot',book+'-ch4',json.dumps({'display_name':name},ensure_ascii=False)))
    conn.commit(); conn.close()
    return db


def fact(identity, subject, predicate, obj, **kwargs):
    return {'id':identity, 'subject':subject,'predicate':predicate,'object':obj,'entity_type':'character',**kwargs}


def test_label_resolution_is_scoped_to_book_even_for_shared_ephemeral_ids(graph_db):
    first = EntityDisplayLabels(graph_db, 'n1')
    second = EntityDisplayLabels(graph_db, 'n2')
    assert first.resolve('ephemeral-4-deadbeef', None, 'character')[0] == '药铺掌柜'
    assert second.resolve('ephemeral-4-deadbeef', None, 'character')[0] == '另一位掌柜'
    label, _, resolved = first.resolve('novel-n2-char-1', None, 'character')
    assert resolved is False
    assert label == '待命名人物'


def test_unknown_ids_keep_identity_and_stay_unresolved_across_roundtrips(graph_db):
    labels = EntityDisplayLabels(graph_db, 'n1')
    original = fact('unknown','ephemeral-9-1234abcd','认识','林风')
    normalized = labels.normalize(original)
    assert normalized['subject'] == '待命名人物（第9章）'
    assert normalized['subject_entity_id'] == original['subject']
    assert labels.normalize(normalized)['attributes']['unresolved_subject_entity'] == 'true'
    assert original['subject'] == 'ephemeral-9-1234abcd'


def test_old_rows_resolve_without_rewriting_original_facts(graph_db):
    conn = sqlite3.connect(graph_db.db_path)
    conn.execute("""INSERT INTO triples(id,novel_id,subject,predicate,object,entity_type,subject_entity_id,object_entity_id)
        VALUES('old','n1','novel-n1-char-1','认识','novel-n1-char-2','character','novel-n1-char-1','novel-n1-char-2')""")
    conn.commit(); conn.close()
    repo = SqliteKnowledgeRepository(graph_db)
    old = repo.get_by_novel_id('n1').facts[0]
    assert (old.subject, old.object) == ('林风','秦柔')
    assert old.subject_entity_id == 'novel-n1-char-1'
    assert graph_db.fetch_one("SELECT subject FROM triples WHERE id='old'")['subject'] == 'novel-n1-char-1'


@pytest.mark.asyncio
async def test_new_writes_store_labels_and_preserve_explicit_endpoint_types(graph_db):
    repo = TripleRepository(graph_db)
    await repo.save(Triple(id='new',novel_id='n1',subject_type='character',subject_id='ephemeral-4-deadbeef',
                          predicate='到访过',object_type='location',object_id='heishi_fang'))
    row = graph_db.fetch_one("SELECT * FROM triples WHERE id='new'")
    assert (row['subject'],row['object']) == ('药铺掌柜','黑市坊')
    assert row['subject_entity_id'] == 'ephemeral-4-deadbeef'
    assert graph_db.fetch_one("SELECT attr_value FROM triple_attr WHERE triple_id='new' AND attr_key='object_type'")['attr_value'] == 'location'


def test_cast_excludes_places_props_roles_and_unknown_ids_but_keeps_people(graph_db, tmp_path):
    repo = SqliteKnowledgeRepository(graph_db)
    repo.save_triples_batch('n1',[
        fact('r1','novel-n1-char-1','认识','novel-n1-char-2'),
        fact('r2','林风','认识','秦柔'),
        fact('visit','ephemeral-4-deadbeef','到访过','heishi_fang',attributes={'object_type':'location'}),
        fact('item','林风','持有','铜钥匙',attributes={'object_type':'item'}),
        fact('wrong-place','黑市坊','隶属','青云宗',attributes={'object_type':'organization'}),
        fact('legacy-group','青云宗','逐出','林风'),
        fact('role','林风','是','主角',attributes={'object_type':'concept'}),
        fact('unknown','ephemeral-9-1234abcd','认识','林风',attributes={'object_type':'character'}),
    ])
    service=CastService(tmp_path,repo,CastRuntimeSettings(graph_cache_ttl_seconds=0))
    graph=service.get_cast_graph('n1')
    names={c.name for c in graph.characters}
    assert {'林风','秦柔','药铺掌柜'} <= names
    assert not names & {'黑市坊','青云宗','铜钥匙','主角','待命名人物（第9章）'}
    assert len(graph.relationships) == 1
    assert service.get_cast_graph('n2').characters == []
