"""Book-scoped entity labels for stored graph IDs; never use another book's names."""
from __future__ import annotations

import json
import re
from typing import Any
from domain.knowledge.entity_display import is_internal_entity_id


class EntityDisplayLabels:
    def __init__(self, db: Any, novel_id: str):
        self.labels: dict[tuple[str, str], str] = {}
        sources = [
            ("SELECT id, name, 'character' AS kind FROM unified_characters WHERE novel_id = ?", "id", "name"),
            ("SELECT id, name, 'character' AS kind FROM bible_characters WHERE novel_id = ?", "id", "name"),
            ("SELECT id, name, 'location' AS kind FROM bible_locations WHERE novel_id = ?", "id", "name"),
            ("SELECT id, name, entity_type AS kind FROM entity_base WHERE novel_id = ?", "id", "name"),
            ("SELECT entity_id, display_label, entity_kind AS kind FROM chapter_entity_mentions WHERE novel_id = ?", "entity_id", "display_label"),
        ]
        for sql, id_key, label_key in sources:
            for row in self._rows(db, sql, novel_id):
                self._remember(row.get("kind"), row.get(id_key), row.get(label_key))
        for row in self._rows(db, """SELECT ce.element_id, ce.notes FROM chapter_elements ce
            JOIN story_nodes sn ON sn.id = ce.chapter_id
            WHERE sn.novel_id = ? AND ce.element_type = 'character'""", novel_id):
            try:
                notes = json.loads(row.get("notes") or "{}")
                if isinstance(notes, dict):
                    self._remember("character", row.get("element_id"), notes.get("display_name") or notes.get("name"))
            except (ValueError, TypeError):
                pass

    @staticmethod
    def _rows(db: Any, sql: str, novel_id: str) -> list[dict]:
        try:
            return [dict(row) for row in db.fetch_all(sql, (novel_id,))]
        except Exception:
            # Optional legacy tables can be absent in old/minimal databases.
            return []

    def _remember(self, kind: Any, entity_id: Any, name: Any) -> None:
        if not isinstance(name, str) or not name.strip() or is_internal_entity_id(name):
            return
        if kind and entity_id:
            self.labels.setdefault((str(kind), str(entity_id)), name.strip())

    def resolve(self, text: str, entity_id: str | None, kind: str | None) -> tuple[str, str | None, bool]:
        raw = (text or "").strip()
        for key in dict.fromkeys([entity_id, raw]):
            if not key:
                continue
            if (kind, key) in self.labels:
                return self.labels[(kind, key)], kind, True
            matches = {(t, name) for (t, eid), name in self.labels.items() if eid == key}
            if len(matches) == 1:
                resolved_kind, label = next(iter(matches))
                return label, resolved_kind, True
        # Legacy facts often stored a display name with the wrong broad
        # subject type. A unique canonical name supplies its actual type.
        name_types = {t for (t, _), label in self.labels.items() if label == raw}
        if len(name_types) == 1:
            return raw, next(iter(name_types)), True
        if is_internal_entity_id(raw) or (is_internal_entity_id(entity_id or "") and raw.startswith("待命名人物")):
            chapter = re.match(r"^ephemeral-(\d+)-", entity_id or raw)
            suffix = f"（第{chapter.group(1)}章）" if chapter else ""
            return "待命名人物" + suffix, kind, False
        return raw, kind, bool(raw)

    def normalize(self, fact: dict) -> dict:
        result = dict(fact)
        attrs = dict(fact.get("attributes") or {})
        for side, type_key in (("subject", "entity_type"), ("object", "object_type")):
            kind = fact.get(type_key) if side == "subject" else attrs.get(type_key)
            label, resolved_kind, resolved = self.resolve(fact.get(side, ""), fact.get(side + "_entity_id"), kind)
            result[side] = label
            if resolved_kind:
                attrs["resolved_" + side + "_type"] = resolved_kind
            attrs[side + "_name_known"] = "true" if any(
                stored_type == resolved_kind and stored_label == label
                for (stored_type, _), stored_label in self.labels.items()
            ) else "false"
            flag = "unresolved_" + side + "_entity"
            attrs.pop(flag, None)
            if not resolved:
                attrs[flag] = "true"
                if not result.get(side + "_entity_id") and is_internal_entity_id(fact.get(side, "")):
                    result[side + "_entity_id"] = fact[side]
        result["attributes"] = attrs
        return result
