"""Explicit subject/action attribution for Chinese prose (Xuheng extension).

Deliberately conservative: ambiguous, quoted, negated and hypothetical actions
are returned as uncertain rather than being assigned to every named character.
Offsets are Unicode code points into the unmodified input, end exclusive.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping


ACTION_LABELS = {
    "fire_magic": "火系", "water_magic": "水系", "ice_magic": "冰系",
    "lightning_magic": "雷系", "wind_magic": "风系",
    "use_sword": "用剑", "use_bow": "射箭", "use_gun": "开枪",
}
MAGIC_TYPES = ("火系", "水系", "冰系", "雷系", "风系")
_KEYWORDS = {
    "火球": "fire_magic", "火系": "fire_magic", "火焰": "fire_magic",
    "水球": "water_magic", "水系": "water_magic", "冰系": "ice_magic",
    "雷电": "lightning_magic", "雷系": "lightning_magic", "风系": "wind_magic",
    "拔剑": "use_sword", "挥剑": "use_sword", "射箭": "use_bow", "开枪": "use_gun",
}
_ACTIONS = re.compile("|".join(map(re.escape, sorted(_KEYWORDS, key=len, reverse=True))))
_CLAUSES = re.compile(r"[^，,。！？!?；;\n]+[，,。！？!?；;\n]?")
_UNCERTAIN = re.compile(r"如果|假如|假设|倘若|若是|也许|可能|打算|准备|计划|试图|想要|据说|听说|传闻|声称")
_NEGATION = re.compile(r"没有|并未|并不|不能|不会|未曾|不曾|禁止|无法|没能|不得|不要|未|没|不")
_MAGIC_VERB = re.compile(r"释放|施放|施展|使出|召唤|凝聚|操纵|操控|发动|唤出|祭出")
_OTHER_SUBJECT = re.compile(r"另一个人|有人|对方|他|她|它|他们|她们")
_CARRIED_PREFIX = re.compile(
    r"^(?:\s|随后|然后|接着|又|便|随即|并|再|立刻|转身|抬手|猛地|突然|于是|继续|却|悄悄|缓缓)*"
    r"(?:释放|施放|施展|使出|召唤|凝聚|操纵|操控|发动|唤出|祭出)?$"
)


@dataclass(frozen=True)
class ActionMention:
    entity_id: str | None
    entity_name: str | None
    action: str
    start: int
    end: int
    quote: str
    reason: str | None = None


def attribute_actions(text: str, names: Mapping[str, str]) -> list[ActionMention]:
    """Associate explicit actions with the nearest unambiguous named subject.

    `names` maps canonical names AND aliases to stable character IDs. Longest
    matches win; aliases shared by several characters must be rejected upstream.
    This is a narrow rule engine, not a general Chinese semantic parser.
    """
    if not text or not names:
        return []
    name_pattern = re.compile("|".join(map(re.escape, sorted(names, key=len, reverse=True))))
    canonical: dict[str, str] = {}
    for name, entity_id in names.items():
        canonical.setdefault(entity_id, name)
    result: list[ActionMention] = []
    carried: tuple[str, str] | None = None
    quote_depth = 0
    for clause in _CLAUSES.finditer(text):
        fragment = clause.group()
        mentions = list(name_pattern.finditer(fragment))
        actions = list(_ACTIONS.finditer(fragment))
        for action in actions:
            before = fragment[:action.start()]
            previous = [m for m in mentions if m.end() <= action.start()]
            subject = previous[-1] if previous else None
            entity_id = names[subject.group()] if subject else (carried[0] if carried else None)
            entity_name = canonical.get(entity_id) if entity_id else None
            bridge = before[subject.end():] if subject else before
            reason = None
            action_key = _KEYWORDS[action.group()]
            if action_key.endswith("_magic") and not _MAGIC_VERB.search(bridge):
                # A mention of a power is not evidence that someone used it.
                continue
            local_quotes = quote_depth + before.count("“") + before.count("「") - before.count("”") - before.count("」")
            if not entity_id:
                reason = "没有可确定的行为主体"
            elif local_quotes > 0 or '"' in before or "'" in before:
                reason = "引述中的行为需要人工确认"
            elif _UNCERTAIN.search(fragment):
                reason = "假设、计划或转述尚不能作为已发生的行为"
            elif _NEGATION.search(bridge):
                # Explicitly negated actions are not positive action candidates.
                continue
            elif _OTHER_SUBJECT.search(bridge):
                reason = "存在代词或其他主体，无法可靠归属"
            elif not subject and not _CARRIED_PREFIX.fullmatch(bridge):
                reason = "省略主体的分句可能引入其他人物，无法可靠归属"
            elif subject and re.search(r"被|让|叫|要求|命令", before[:subject.start()]):
                reason = "被动或使令句需要人工确认"
            elif subject and re.search(r"被|让|要求|命令|看见|看到|听见|躲开|挡住", bridge):
                reason = "被动、使令或感知描述需要人工确认"
            elif subject and len(previous) > 1:
                relation = before[previous[-2].end():subject.start()]
                if re.search(r"和|与|及|、|对|向|朝|给|把|被|让|替|帮", relation):
                    reason = "多人关系中的动作主体不明确"
            start = clause.start() + (subject.start() if subject else 0)
            end = clause.start() + action.end()
            result.append(ActionMention(
                entity_id, entity_name, action_key, start, end, text[start:end], reason,
            ))
        if mentions:
            last = mentions[-1]
            carried = (names[last.group()], canonical[names[last.group()]]) if len({names[m.group()] for m in mentions}) == 1 else None
        elif _OTHER_SUBJECT.search(fragment):
            carried = None
        quote_depth = max(0, quote_depth + fragment.count("“") + fragment.count("「") - fragment.count("”") - fragment.count("」"))
        if fragment.endswith(tuple("。！？!?；;\n")):
            carried = None
    return result
