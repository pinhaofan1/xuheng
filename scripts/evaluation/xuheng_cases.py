"""80 authored synthetic regression samples, NOT independently labeled data.

Development and evaluation characters differ. The evaluator set is fixed for
repeatable comparisons, but was authored alongside the engine and is not a
blind generalization benchmark. Some challenge cases intentionally exceed scope.
"""
from application.audit.evidence_models import CharacterRule, RuleSet


DEV_RULES = RuleSet(profiles=[
    CharacterRule(id="a", name="林烬", aliases=["阿烬"], allowed_magic=["火系"]),
    CharacterRule(id="b", name="沈澜", aliases=["阿澜"], allowed_magic=["水系"]),
])
TEST_RULES = RuleSet(profiles=[
    CharacterRule(id="a", name="苏檀", aliases=["小檀"], allowed_magic=["火系"]),
    CharacterRule(id="b", name="顾岚", aliases=["老顾"], allowed_magic=["水系"]),
])

# Each expected pair is (stable subject ID, action). Duplicates within one case
# count once; this evaluates actor/action conclusions, not occurrence matching.
DEVELOPMENT = [
    ("林烬释放水球。", [("a", "water_magic")]),
    ("沈澜施展火系法术。", [("b", "fire_magic")]),
    ("林烬施展冰系法术。", [("a", "ice_magic")]),
    ("沈澜释放雷电。", [("b", "lightning_magic")]),
    ("林烬召唤风系法术。", [("a", "wind_magic")]),
    ("林烬释放火球，沈澜施展水系法术。", []),
    ("林烬没有释放水球。", []),
    ("沈澜不会释放火球。", []),
    ("林烬站在山脚，沈澜走向城门。", []),
    ("林烬看着远处的水球。", []),
    ("阿烬施放水球。", [("a", "water_magic")]),
    ("阿澜释放火球。", [("b", "fire_magic")]),
    ("林烬施展水系法术，沈澜施展火系法术。", [("a", "water_magic"), ("b", "fire_magic")]),
    ("林烬离开了。沈澜释放火球。", [("b", "fire_magic")]),
    ("沈澜施展水系法术，然后释放火球。", [("b", "fire_magic")]),
    ("如果林烬释放水球，就能灭火。", []),
    ("沈澜声称自己释放火球。", []),
    ("林烬和沈澜施展水系法术。", []),
    ("沈澜说：“我释放火球。”", []),
    ("他释放火球。", []),
]

POSITIVE = [
    ("苏檀推开木窗，苏檀释放水球，浇灭了院中的余火。", [("a", "water_magic")]),
    ("顾岚在屋脊停下，顾岚施展火系法术，封住了退路。", [("b", "fire_magic")]),
    ("苏檀施放冰系魔法，霜沿着石栏蔓延。", [("a", "ice_magic")]),
    ("顾岚凝聚雷电，电光击穿了夜色。", [("b", "lightning_magic")]),
    ("苏檀召唤风系法术，将落叶吹回庭院。", [("a", "wind_magic")]),
    ("🌊顾岚释放火球。", [("b", "fire_magic")]),
    ("小檀施展水系法术，渡口传来急促的脚步声。", [("a", "water_magic")]),
    ("老顾发动冰系法术，脚下的泥水结成了冰。", [("b", "ice_magic")]),
    ("苏檀使出雷系法术，铁门随之震动。", [("a", "lightning_magic")]),
    ("顾岚操控风系魔法，小舟慢慢转向。", [("b", "wind_magic")]),
    ("苏檀抬起手掌，随后释放水球。", [("a", "water_magic")]),
    ("顾岚释放水球，然后释放火球。", [("b", "fire_magic")]),
    ("苏檀祭出冰系法术，白霜压低了枝头。", [("a", "ice_magic")]),
    ("顾岚唤出火焰，火光映亮了旧墙。", [("b", "fire_magic")]),
    ("苏檀操纵水球，水珠穿过了门环。", [("a", "water_magic")]),
    ("雨停后，顾岚施放雷电，屋檐上的水滴齐齐落下。", [("b", "lightning_magic")]),
    ("苏檀凝聚风系法术，众人屏住了呼吸。", [("a", "wind_magic")]),
    ("顾岚召唤火球。火光照着渡口的名字。", [("b", "fire_magic")]),
    # Challenge: action verb outside the deliberately narrow verb dictionary.
    ("顾岚打出火球，船帆立刻燃烧起来。", [("b", "fire_magic")]),
    ("苏檀手掌翻起，一枚水球成形，随后飞向长街。", [("a", "water_magic")]),
]

NEGATIVE = [
    ("苏檀释放火球，火光照见了脚边的足迹。", []),
    ("顾岚施展水系法术，水声盖住了谈话。", []),
    ("小檀凝聚火焰，守住了破损的窗。", []),
    ("老顾召唤水球，洗去了石碑上的泥。", []),
    ("苏檀没有释放水球，而是沿着墙根离开。", []),
    ("顾岚并未施展火系法术，庭院依旧漆黑。", []),
    ("苏檀不曾召唤风系法术。", []),
    ("顾岚无法释放雷电，只能等待援手。", []),
    ("苏檀看见水球，向后退了一步。", []),
    ("顾岚躲开火球，衣角擦过了门框。", []),
    ("苏檀站在水边，顾岚坐在树下。", []),
    ("苏檀收起旧地图。风吹过长街。", []),
    ("顾岚注视着火焰，想起了往日的朋友。", []),
    ("苏檀擅长火系法术，今天没有出手。", []),
    ("顾岚施展水系法术；苏檀凝聚火焰。", []),
    ("苏檀释放火球，然后释放火球照明。", []),
    ("顾岚没有犹豫，随后施展水系法术。", []),
    ("火球落在顾岚面前，顾岚向后退去。", []),
    ("苏檀翻开书页。书上写着：水球术。", []),
    ("顾岚吸收火焰，手中的护符发出微光。", []),
]

MULTI = [
    ("苏檀释放火球，顾岚施展水系法术。", []),
    ("苏檀施展水系法术，顾岚释放火球。", [("a", "water_magic"), ("b", "fire_magic")]),
    ("苏檀站在岸边。顾岚释放火球。", [("b", "fire_magic")]),
    ("顾岚看向苏檀，苏檀施展水系法术。", [("a", "water_magic")]),
    ("苏檀召唤火球，顾岚静静等待。", []),
    ("顾岚施展水系法术，苏檀没有释放水球。", []),
    ("苏檀释放水球帮助顾岚。", [("a", "water_magic")]),
    ("顾岚坐下，苏檀使出雷系法术。", [("a", "lightning_magic")]),
    ("小檀释放火球，老顾释放水球。", []),
    ("苏檀的身旁，顾岚施展水系法术。", []),
]

UNCERTAIN = [
    ("苏檀和顾岚释放火球。", []),
    ("苏檀对顾岚施展火系法术。", []),
    ("假如苏檀释放水球，火势就会变小。", []),
    ("顾岚计划释放火球引开守卫。", []),
    ("据说苏檀施展水系法术，救下了陌生人。", []),
    ("顾岚说：“我释放火球的时候，天还没有亮。”", []),
    ("苏檀停下脚步，他释放水球。", []),
    # Challenge cases: indirect memory, external item and unreal illusion.
    ("顾岚想起苏檀释放水球的那一夜。", []),
    ("顾岚借用火符释放火球，护符随后化为了灰烬。", []),
    ("苏檀释放水球，但那只是旁人眼中的幻术。", []),
]


def cases():
    result = []
    for split, category, items in [
        ("development", "development", DEVELOPMENT), ("evaluation", "positive", POSITIVE),
        ("evaluation", "negative", NEGATIVE), ("evaluation", "multi", MULTI),
        ("evaluation", "uncertain", UNCERTAIN),
    ]:
        for index, (text, expected) in enumerate(items, 1):
            result.append({"id": f"{category}-{index:02}", "split": split, "category": category,
                           "text": text, "expected": expected, "expect_uncertain": category == "uncertain"})
    return result
