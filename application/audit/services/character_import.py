"""Preview existing character facts without inventing constraints or saving them."""
from pydantic import ValidationError

from application.audit.evidence_models import CharacterRule, text_hash


def preview_character_rules(store, novel_id: str) -> dict:
    profiles, warnings, names = [], [], set()
    rows = store.character_rows(novel_id)
    for row in rows[:64]:
        name = str(row['name'] or '').strip()
        if name in names:
            warnings.append(f'人物姓名重复，未自动合并：{name}')
            continue
        facts = []
        for key, label in [('description','人物简介'),('gender','性别'),('age','年龄'),('appearance','外貌'),
                           ('personality','性格'),('background','背景'),('core_belief','信念')]:
            value = str(row.get(key) or '').strip()
            if value:
                fact = f'{label}：{value}'
                if len(fact) > 300:
                    warnings.append(f'{name}的{label}超过单条长度，预览仅保留前 300 字，请核对')
                facts.append(fact[:300])
        try:
            profile = CharacterRule(id='bible-'+text_hash(str(row['id']))[:20],name=name,facts=facts)
        except ValidationError:
            warnings.append('某个人物名称不符合审校格式，未导入；请在创作人物库中核对')
            continue
        names.add(name)
        profiles.append(profile.model_dump())
    if len(rows) > 64:
        warnings.append('最多预览 64 个人物，其他人物未导入')
    return {'profiles':profiles,'warnings':warnings,'source':'作品人物库；只返回草稿，未保存审校规则'}
