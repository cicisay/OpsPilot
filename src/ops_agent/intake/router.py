"""规则版意图识别：正则 + 关键词 + 槽位抽取。

三条硬约束（PRD Step 1 的禁止项）：
1. 不调 LLM —— 纯规则，结果可复现；
2. 不读文件 —— 本模块只接受字符串；
3. 不产生授权行为 —— 这里判出来的 ``toolset`` 只是声明，不落到任何网关。

打分方式：每个意图由若干条 ``(正则, 权重, 说明)`` 组成，命中即加分。
权重只有四档，含义固定，方便后续 Step 2 看错例时判断"错在规则还是错在语料"：

- 2.5 = 决定性信号（"批量"、"翻译"、"记住" 这类一旦出现基本就定了）
- 1.5 = 强信号（"多少"、"标题" 这类主题词）
- 1.0 = 动作词（"上架"、"看看"，方向明确但多个意图共用）
- 0.6 / 0.8 = 辅助信号（时间段、量词、物料名）

置信度换算：``base = min(0.95, 0.50 + 0.105 * score)``，再按与次优意图的分差打折。
``score < 1.0`` 视为"没听出方向"，直接 CLARIFY；分差小于 25% 视为歧义，也 CLARIFY。
"""

from __future__ import annotations

import re
import unicodedata

from .schema import INTENT_SPECS, Intent, IntentResult

# 超过这个长度只匹配前 2000 字，避免有人在输入框里粘一整本手册。
MAX_TEXT_CHARS = 2000
# 低于这个分数说明只有动作词、没有对象，还不足以定意图（"上架" 就该反问）。
MIN_SIGNAL = 1.5
# 前两名相差不到 25% 视为歧义。
AMBIGUITY_RATIO = 0.75
# 单条意图到这个分数才算"强意图"，复合委托要求至少两条。
COMPOSITE_SCORE = 2.0
# 复合委托里两件事分数差不到 30%，就按出现先后定主次。
COMPOSITE_TIE_RATIO = 0.7
# 置信度低于这个值必须转 CLARIFY（PRD 验收口径）。
CONFIDENCE_FLOOR = 0.6

# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------

# 零宽、软连字符、bidi 控制符：复制粘贴和某些输入法会带进来，肉眼看不见，
# 但会把 "上\u200b架" 这种词拆开，导致正则匹配不到。
_INVISIBLE = dict.fromkeys(
    map(ord, "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\u00ad\u202a\u202b\u202c\u202d\u202e"),
    None,
)


def normalize(text: str) -> str:
    """NFKC（全角→半角）+ 去不可见字符 + 压空白 + 转小写。

    只做归一化，不改写语义：``这２０个`` → ``这20个``，``ＮＩＫＥ`` → ``nike``。
    """
    folded = unicodedata.normalize("NFKC", text)
    folded = folded.translate(_INVISIBLE)
    folded = re.sub(r"\s+", " ", folded)
    return folded.strip().lower()


# ---------------------------------------------------------------------------
# 词表
# ---------------------------------------------------------------------------

# 容错间隔：允许跨逗号（"标题太长了，帮我改短"），但不跨句末标点
_CLAUSE = r"[^。;；!！?？\n]{0,14}"

# 上传类动作。"上到" 是口语（"上到美国站"），必须收进来，否则最常见的委托识别不到。
# "铺" 要排除"店铺/铺面"——那是名词，不是"铺货"。
_UPLOAD_VERB = (
    r"(?:上(?:架|新|传|到|线)|传(?:到|上|给|进)|(?<!店)铺(?:货|到|上)?|导入|"
    r"建链|建[^，。]{0,8}listing|发布|发到|推到)"
)

# 复数/批量的限定词
_DET_BATCH = r"(?:批量|一批|整批|这批|那批|这波|所有|全部|多个|若干|\d{1,4}\s*(?:个|款|条|件|套))"

# 中文数字字符类，规则表与槽位抽取共用一份，避免两处写得不一样
_CN_CLASS = r"[一二两三四五六七八九十仨俩]"

# 单数限定词。"这个月/这个季度" 指的是时间段不是单个商品，要用否定前瞻排掉。
# 明确的单数指代，语义强度高于裸"这个"
_DET_SINGLE_STRONG = r"(?:这款|这一款|这一个|这条|那条|单个|单独)"
# 弱限定词。强的那几个已经由 _DET_SINGLE_STRONG 单独计分，这里不能重复出现，
# 否则"这条"会同时命中两条规则、被算两遍（体检那条就是这么被压下去的）。
_DET_SINGLE = r"(?:这个(?!月|季度|周|星期|礼拜)|(?:一|1)\s*(?:个|款|条))"
# 共现规则里要认全部单数说法，但只算一次分——所以单独拼一个只给 co-occurrence 用的并集。
_DET_SINGLE_ANY = rf"(?:{_DET_SINGLE_STRONG}|{_DET_SINGLE})"

_RULES: tuple[tuple[Intent, tuple[tuple[str, float, str], ...]], ...] = (
    (
        Intent.BATCH_UPLOAD,
        (
            (rf"{_DET_BATCH}{_CLAUSE}{_UPLOAD_VERB}", 2.5, "批量限定 + 上传动作"),
            (rf"{_UPLOAD_VERB}{_CLAUSE}{_DET_BATCH}", 2.5, "上传动作 + 批量限定（倒装）"),
            (
                rf"上\s*(?:这|那)?\s*(?:\d+|{_CN_CLASS})\s*(?:个|款|条)?\s*(?:新品|产品|商品|链接|sku)",
                2.0,
                "上 + 数量 + 商品（口语省略）",
            ),
            (_DET_BATCH, 1.0, "批量限定词"),
            (r"铺货|铺到|铺上", 1.5, "铺货动作"),
            (r"新品|新链接|新商品|新\s*sku|供应商|资料包|选品表|铺货表", 0.8, "上新物料"),
            (_UPLOAD_VERB, 1.0, "上传动作"),
        ),
    ),
    (
        Intent.SINGLE_UPLOAD,
        (
            (rf"{_DET_SINGLE_ANY}{_CLAUSE}{_UPLOAD_VERB}", 2.5, "单数限定 + 上传动作"),
            (rf"{_UPLOAD_VERB}{_CLAUSE}{_DET_SINGLE_ANY}", 2.5, "上传动作 + 单数限定（倒装）"),
            (r"上(?:这|那)?(?:一|1)\s*(?:个|款|条)", 2.0, "上 + 单数（口语省略）"),
            (r"(?:挑|选|拿)\s*(?:一|1)\s*(?:个|款|条)", 1.5, "挑一个款（口语）"),
            (_DET_SINGLE_STRONG, 1.5, "明确单数限定词"),
            (_DET_SINGLE, 0.8, "单数限定词"),
            (r"一条链接|单独上|只上", 0.8, "单件口吻"),
        ),
    ),
    (
        Intent.LISTING_DIAGNOSE,
        (
            (r"诊断|体检|复盘|会诊|找原因|分析一下|排查|看看问题", 2.5, "诊断动作"),
            (
                r"不出单|卖不动|没流量|流量下滑|掉量|没曝光|没点击|曝光低|曝光一直很低|"
                r"转化差|转化率低|点击率低|退货高|退货率高|库存积压|滞销|动销差|压货|"
                r"下滑|上不去|低这么多|异常",
                2.5,
                "具体症状",
            ),
            (
                r"检查",
                1.5,
                "检查店铺/商品动作",
            ),
            # "检查 + 对象" 是完整的诊断动作，比光秃秃的"检查"可信得多。
            # 用户实测："帮我检查店铺滞后销商品"（打字变形，症状词没命中）
            # 只靠 1.5 的"检查"过不了置信度地板，直接掉 CLARIFY。
            (
                rf"检查{_CLAUSE}(?:店铺|店里|店|商品|链接|listing)",
                2.5,
                "检查 + 对象",
            ),
            (
                r"为什么|为啥|怎么回事|什么原因|是什么原因|问题出在|问题在哪|"
                r"哪里出问题|哪里有问题|有问题",
                2.0,
                "疑问/异常口吻",
            ),
            (r"看看|看一下|查一下|瞅一眼", 0.6, "查看动作"),
        ),
    ),
    (
        Intent.LISTING_REWRITE,
        (
            (
                r"(?:重写|改写|重新写|重做|润色|优化|改进|调整|改|换)"
                rf"{_CLAUSE}(?:标题|五点|详情|描述|文案|卖点|bullet|listing)",
                2.5,
                "改写动作 + 文案部位",
            ),
            (
                rf"(?:标题|五点|详情|描述|文案|卖点|bullet){_CLAUSE}"
                r"(?:重写|改写|重新写|重做|润色|优化|改进|调整|改|换)",
                2.5,
                "文案部位 + 改写动作（倒装）",
            ),
            (r"标题|五点|详情页|详情|描述|文案|卖点|bullet", 0.8, "文案部位"),
        ),
    ),
    (
        Intent.MULTILANG_CONTENT,
        (
            (r"翻译|译成|译为|翻成|翻为|本地化|多语言|多语种|multilingual|locali[sz]ation|l10n", 2.5, "翻译动作"),
            (
                r"(?:德语|英语|英文|日语|法语|西班牙语|西语|意大利语|荷兰语|波兰语)版",
                1.5,
                "X 语版本",
            ),
            (
                r"德语|英语|英文|日语|法语|西班牙语|西语|意大利语|荷兰语|波兰语|"
                r"土耳其语|葡萄牙语|阿拉伯语|俄语",
                1.2,
                "目标语言",
            ),
            (r"小语种|出海版|跨境版", 0.8, "多语场景"),
        ),
    ),
    (
        Intent.MEDIA_COMPOSE,
        (
            (
                r"主图|场景图|白底图|详情图|图片|素材|短视频|视频|海报|banner|"
                r"拍摄|作图|美工|抠图|换背景|a\+页面",
                2.5,
                "素材类型",
            ),
            (rf"(?:做|生成|合成|拍|剪|出|换){_CLAUSE}(?:图|视频|素材|海报)", 1.5, "制作动作 + 素材"),
            (r"一套|几张|一组|张数", 0.6, "素材数量"),
        ),
    ),
    (
        Intent.DATA_QUERY,
        (
            (
                r"多少|(?<![零一二三四五六七八九十百千万])几个|几条|哪些|排名|排行|报表|统计|"
                r"汇总|环比|同比|趋势|对比|占比",
                1.5,
                "问数句式",
            ),
            # "哪些商品滞销" 这类问清单：症状词会把 DIAGNOSE 抬到 2.5，
            # 但用户要的是名单不是诊断，必须压回去。
            # 不收"几个"——"三十几个款"是数量不是问句，会把 E002 抢成 DATA_QUERY。
            (
                r"(?:哪些|哪几|多少)\s*(?:商品|产品|链接|款|sku)",
                2.0,
                "问清单",
            ),
            (
                r"销售额|销量|gmv|曝光|点击率|点击|转化率|转化|退货率|退货|"
                r"毛利率|毛利|库存天数|库存|评分|review|表现|业绩|数据",
                1.5,
                "指标名",
            ),
            (r"上周|本周|这个月|\d+\s*月|最近\s*\d+\s*天|近\s*\d+\s*天", 0.6, "时间范围"),
            (r"(?:出|拉|导)\s*(?:个|一)?\s*(?:报表|报告|数据表)", 2.0, "要报表"),
        ),
    ),
    (
        Intent.MEMORY_MANAGE,
        (
            (r"记住|记一下|记下|记牢|别忘了|备忘", 2.5, "要求记住"),
            (r"忘掉|忘记|删掉(?:这|那)条|删除记忆|撤销(?:这|那)条|别再用这条", 2.5, "要求忘掉"),
            (r"一律|统一|默认|不要再|不许|不准|禁止|别再", 2.5, "强约束口吻"),
            (r"以后[^。，]{0,10}(?:不|别|都|一律|统一)", 1.5, "长期否定/统一口吻"),
            (r"以后|今后|下次|长期|一直", 1.0, "长期性口吻"),
            (r"记忆|偏好|习惯|店规|规则", 1.0, "记忆名词"),
        ),
    ),
    (
        Intent.PRICE_SUGGEST,
        (
            (r"定价|调价|涨价|降价|报价|售价|价格带|定价策略|该卖多少", 2.5, "定价动作"),
            # 裸"价格"只给低权重，"价格带/价格档"已由上面的 2.5 覆盖，用前瞻避开重复计分。
            (r"价格(?!带|档|区间|战)|毛利|利润率|毛利率|成本", 1.0, "价格/利润口径"),
            (r"合适|合理|建议|参考价", 1.0, "征求意见"),
            (r"偏高|偏低|太贵|太便宜|贵了|便宜了", 1.5, "价格判断"),
        ),
    ),
    (
        Intent.CUSTOMER_OUTREACH,
        (
            (r"客户|买家|询盘|站内信|邮件|催评|邀评|差评|售后|投诉|退款申请|纠纷", 1.5, "触达对象/场景"),
            (
                r"写(?:一)?封|发(?:一)?封|回一条|回一下|回复一下|怎么回|帮我回|起草|发一批|群发|"
                r"跟进|触达|联系一下|安抚|解释",
                2.0,
                "触达动作",
            ),
        ),
    ),
)

# 只表示"在派活但没说清派什么"的动词。命中它但不命中领域词 → 反问，不是丢弃。
_VAGUE_TASK = re.compile(r"弄|搞|处理|改|调整|整理|优化一下|看看|看一下|看下|来一?个|方案|重新来|弄好")

# 文案部位与素材部位是两条完全不同的生产线（改写 vs 做图）。一句"弄弄这个标题和图片"
# 两边都点到了，动作却是"弄"——这时候按分数高的那个（素材 2.5）直接派单，
# 等于替用户决定先做图。这类必须反问：先做哪件。
_COPY_PART = re.compile(r"标题|五点|详情页|详情|描述|文案|卖点|bullet")
_MEDIA_PART = re.compile(r"主图|场景图|白底图|详情图|图片|素材|短视频|视频|海报|banner")

# UNKNOWN 兜底时用来排"最接近的意图"。只做排序，不参与打分。
_FALLBACK_HINTS: dict[Intent, tuple[str, ...]] = {
    Intent.BATCH_UPLOAD: ("新品", "批量", "上架", "铺货"),
    Intent.SINGLE_UPLOAD: ("这个", "这款", "一条"),
    Intent.LISTING_DIAGNOSE: ("诊断", "为什么", "不出单", "下滑"),
    Intent.LISTING_REWRITE: ("标题", "文案", "重写", "优化"),
    Intent.MULTILANG_CONTENT: ("翻译", "德语", "英语", "本地化"),
    Intent.MEDIA_COMPOSE: ("主图", "图片", "视频", "素材"),
    Intent.DATA_QUERY: ("数据", "销售额", "曝光", "排名"),
    Intent.MEMORY_MANAGE: ("记住", "以后", "偏好", "默认"),
    Intent.PRICE_SUGGEST: ("定价", "价格", "毛利", "调价"),
    Intent.CUSTOMER_OUTREACH: ("客户", "买家", "询盘", "邮件"),
}

_COMPOSITE = re.compile(r"然后|接着|再|并且|同时|顺便|以及|另外|之后|，|,")


def _compile(
    rules: tuple[tuple[Intent, tuple[tuple[str, float, str], ...]], ...]
) -> tuple[tuple[Intent, tuple[tuple[re.Pattern, float, str], ...]], ...]:
    def _one(item: tuple[str, float, str]) -> tuple[re.Pattern, float, str]:
        pattern, weight, label = item
        return re.compile(pattern, re.IGNORECASE), weight, label

    return tuple(
        (intent, tuple(_one(item) for item in items))
        for intent, items in rules
    )


_COMPILED = _compile(_RULES)


# ---------------------------------------------------------------------------
# 槽位抽取
# ---------------------------------------------------------------------------

_MARKET = (
    (re.compile(r"美国|美站|美区|北美|\bus\b|\busa\b"), "US"),
    (re.compile(r"德国|德站|欧洲站|欧洲|\bde\b|\bgermany\b"), "DE"),
    (re.compile(r"英国|英站|\buk\b|\bbritain\b"), "UK"),
)

_LANG_BRIEF = re.compile(r"([英德法日西]{2,4})(?:三|两|四|五)?语")
_LANG_CHARS = {"英": "en", "德": "de", "法": "fr", "日": "ja", "西": "es"}

_LANGUAGES = (
    (re.compile(r"英语|英文|english"), "en"),
    (re.compile(r"德语|德文|german"), "de"),
    (re.compile(r"日语|日文|japanese"), "ja"),
    (re.compile(r"法语|法文|french"), "fr"),
    (re.compile(r"西班牙语|西语|spanish"), "es"),
    (re.compile(r"意大利语|italian"), "it"),
)

_QTY_DIGIT = re.compile(r"(\d{1,4})\s*(?:个|款|条|件|sku)")
_QTY_CN = re.compile(r"([一二两三四五六七八九十仨俩]{1,3})\s*(?:个|款|条|件)")
# "仨/俩" 后面可以不带量词："上仨新品"
_QTY_CN_BARE = re.compile(r"([仨俩])\s*(?:新品|产品|商品|链接|sku)")
# SKU 有两种写法：前缀写死的 "SKU-123"，和卖家自有编码 "FT-001"。
# 后者不能用 \b 收边——中文紧贴字母时 \b 不成立（"把FT-001的" 里 F 前是汉字），
# 只能用"前后不是字母数字"来判定边界。
_SKU_NUMERIC = re.compile(r"sku[-_\s]?(\d{1,6})", re.IGNORECASE)
_SKU_CODE = re.compile(r"(?<![a-z0-9])([a-z]{2,6}[-_]\d{2,6})(?![a-z0-9])", re.IGNORECASE)
_SOURCE_PATH = re.compile(r"((?:[a-z]:)?[\w./\\-]+\.(?:csv|xlsx|xls|json|txt|zip))")

_FIELD = re.compile(r"标题|五点|详情页|详情|描述|文案|卖点|bullet")
_PROBLEM = re.compile(
    r"不出单|卖不动|没流量|流量下滑|掉量|没曝光|曝光低|转化差|转化率低|点击率低|"
    r"退货高|库存积压|异常"
)
_METRIC = re.compile(
    r"销售额|销量|gmv|曝光|点击率|点击|转化率|转化|退货率|退货|毛利率|毛利|"
    r"库存天数|库存|评分|review|表现|业绩"
)
_TOPIC = re.compile(r"催评|邀评|差评|售后|投诉|退款|询盘|纠纷")
_ASSET = re.compile(r"主图|场景图|白底图|详情图|图片|素材|短视频|视频|海报|banner")

_CN_DIGITS = {
    "仨": 3,
    "俩": 2,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}

# 永远随结果返回的补充信息：规格表的 slots 里没有，但 Step 3 拆单、预览都要用。
_ALWAYS_KEEP = ("quantity", "sku")


def _cn_to_int(token: str) -> int | None:
    """只处理到 99 的中文数字：十二 / 二十 / 二十三。够用了，不引第三方库。"""
    if token in _CN_DIGITS:
        return _CN_DIGITS[token]
    if len(token) == 2:
        if token[0] == "十":
            return 10 + _CN_DIGITS.get(token[1], 0)
        if token[1] == "十":
            return _CN_DIGITS.get(token[0], 0) * 10
    if len(token) == 3 and token[1] == "十":
        return _CN_DIGITS.get(token[0], 0) * 10 + _CN_DIGITS.get(token[2], 0)
    return None


def _extract_quantity(body: str) -> int | None:
    match = _QTY_DIGIT.search(body)
    if match:
        value = int(match.group(1))
        return value if value > 0 else None
    match = _QTY_CN.search(body) or _QTY_CN_BARE.search(body)
    if match:
        return _cn_to_int(match.group(1))
    return None


def _extract_slots(body: str, raw_trimmed: str, spec: dict) -> dict:
    """从文本里抽槽位。只放有证据的值，抽不到就不放。"""
    found: dict = {}

    for pattern, value in _MARKET:
        if pattern.search(body):
            found["market"] = value
            break

    languages: list[str] = []
    for pattern, value in _LANGUAGES:
        if pattern.search(body):
            languages.append(value)
    if not languages:
        # "英德法三语" 这类缩写：逐字拆，只认已知的五个语种首字。
        brief = _LANG_BRIEF.search(body)
        if brief:
            languages = [_LANG_CHARS[ch] for ch in brief.group(1) if ch in _LANG_CHARS]

    quantity = _extract_quantity(body)
    if quantity:
        found["quantity"] = quantity

    sku = _SKU_NUMERIC.search(body)
    if sku:
        found["sku"] = f"SKU-{sku.group(1).upper()}"
    else:
        code = _SKU_CODE.search(body)
        if code:
            # 自有编码原样保留（"FT-001" 就是它的真名），只把分隔符统一成连字符。
            found["sku"] = code.group(1).upper().replace("_", "-")

    path = _SOURCE_PATH.search(body)
    if path:
        found["source_path"] = path.group(1)

    field = _FIELD.search(body)
    if field:
        found["field"] = field.group(0)

    problem = _PROBLEM.search(body)
    if problem:
        found["problem"] = problem.group(0)

    metrics: list[str] = []
    for match in _METRIC.finditer(body):
        if match.group(0) not in metrics:
            metrics.append(match.group(0))
    if metrics:
        found["metrics"] = metrics

    topic = _TOPIC.search(body)
    if topic:
        found["topic"] = topic.group(0)

    assets: list[str] = []
    for match in _ASSET.finditer(body):
        if match.group(0) not in assets:
            assets.append(match.group(0))
    if assets:
        found["asset_types"] = assets

    # 记忆类：动作 + 原文。原文就是"要记住的那句话"，不加工、不摘要。
    if re.search(r"记住|记一下|记下|记牢|备忘", body):
        found["action"] = "add"
        found["content"] = raw_trimmed
    elif re.search(r"忘掉|忘记|删掉(?:这|那)条|删除记忆|撤销(?:这|那)条", body):
        found["action"] = "revoke"
        found["content"] = raw_trimmed
    elif re.search(r"默认|一律|统一|禁止|不要再|不许|不准|别再|以后[^。，]{0,10}(?:不|别|都)", body):
        # "以后报价都按美元算" 没有"记住"两个字，但语义就是长期规则。
        found["action"] = "add"
        found["content"] = raw_trimmed
    elif re.search(r"记忆|偏好|习惯", body):
        found["action"] = "list"

    keep = set(spec["slots"]) | set(_ALWAYS_KEEP)
    slots = {key: value for key, value in found.items() if key in keep}

    # language 是单数槽位（BATCH/SINGLE 用），languages 是复数槽位（多语种用）。
    if languages:
        if "languages" in keep:
            slots["languages"] = languages
        if "language" in keep and len(languages) == 1:
            slots["language"] = languages[0]

    return slots


def _missing_slots(spec: dict, slots: dict, defaults: dict | None) -> list[str]:
    """必填槽位里还缺哪些。defaults 能给的值不算缺，但也不会被塞进 slots。"""
    available = set(slots) | set(defaults or {})
    return [name for name in spec["slots"] if name not in available]


# ---------------------------------------------------------------------------
# 打分
# ---------------------------------------------------------------------------

def _score(body: str) -> dict[Intent, tuple[float, int]]:
    """返回 {意图: (总分, 最早命中位置)}。

    位置用来在同分时决定主次：复合委托里 "先上架再优化" 的主意图应该是先说的那个。
    """
    scores: dict[Intent, tuple[float, int]] = {}
    for intent, rules in _COMPILED:
        total = 0.0
        first = -1
        for pattern, weight, _label in rules:
            match = pattern.search(body)
            if match:
                total += weight
                if first < 0 or match.start() < first:
                    first = match.start()
        if total > 0:
            scores[intent] = (round(total, 2), first)
    return scores


def _base_confidence(score: float) -> float:
    return min(0.95, 0.50 + 0.105 * score)


def _fallback_candidates(body: str, limit: int = 3) -> list[str]:
    """没命中任何规则时，按关键词重合度给"最接近的意图"。"""
    scored = []
    for intent, hints in _FALLBACK_HINTS.items():
        hits = sum(1 for hint in hints if hint in body)
        if hits:
            scored.append((hits, intent))
    scored.sort(key=lambda item: (-item[0], item[1].value))
    return [intent.value for _hits, intent in scored[:limit]]


def explain(text: str) -> dict[str, list[str]]:
    """返回每个意图命中了哪些规则，给评测脚本看错例用（Step 2 需要"原因"）。"""
    body = normalize((text or "").strip()[:MAX_TEXT_CHARS])
    detail: dict[str, list[str]] = {}
    for intent, rules in _COMPILED:
        hits = [label for pattern, _weight, label in rules if pattern.search(body)]
        if hits:
            detail[intent.value] = hits
    return detail


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def classify(text: str, defaults: dict | None = None) -> IntentResult:
    """一句话 → 意图 + 槽位 + 缺什么。

    ``defaults`` 是店铺级默认值（如 ``{"market": "US", "language": "en"}``）：
    它能压掉 ``missing``，但**不会**被写进 ``slots``——"文里说过" 和 "店里默认"
    必须能分开，否则 Step 3 标不出 assumed。
    """
    raw = "" if text is None else str(text)
    trimmed = raw.strip()

    if not trimmed:
        # 空输入不是"听不懂"，是"没说"，用 UNKNOWN + 0 置信度区分开。
        return IntentResult(intent=Intent.UNKNOWN, confidence=0.0, raw_text=raw)

    body = normalize(trimmed[:MAX_TEXT_CHARS])
    scores = _score(body)

    if not scores:
        # 没有领域词、但能看出是在派活（"帮我处理一下"），该反问一句；
        # 纯噪声（"今天天气不错"）才回 UNKNOWN。两者都是 SAFE，但语义不同。
        if _VAGUE_TASK.search(body):
            return IntentResult(
                intent=Intent.CLARIFY,
                confidence=0.30,
                candidates=_fallback_candidates(body),
                raw_text=raw,
            )
        return IntentResult(
            intent=Intent.UNKNOWN,
            confidence=0.0,
            candidates=_fallback_candidates(body),
            raw_text=raw,
        )

    ranked = sorted(scores.items(), key=lambda item: (-item[1][0], item[1][1], item[0].value))
    best_intent, (best_score, _best_pos) = ranked[0]
    second_score = ranked[1][1][0] if len(ranked) > 1 else 0.0
    spec = INTENT_SPECS[best_intent]
    slots = _extract_slots(body, trimmed, spec)

    # 1) 只有辅助信号，方向不明。
    if best_score < MIN_SIGNAL:
        return IntentResult(
            intent=Intent.CLARIFY,
            confidence=round(min(CONFIDENCE_FLOOR - 0.01, _base_confidence(best_score)), 2),
            slots=slots,
            candidates=[intent.value for intent, _detail in ranked[:2]],
            raw_text=raw,
        )

    # 2) 复合委托：两句以上强意图 + 连接词。保留主意图，其余进 candidates，
    #    这样 Step 5 能提示"还听出了 X"，而不是直接打回让用户重说。
    strong = [(intent, detail) for intent, detail in ranked if detail[0] >= COMPOSITE_SCORE]
    if len(strong) >= 2 and _COMPOSITE.search(body):
        # 两件事分数咬得近时，按"先说的先做"定主任务：
        # "先上架，再改标题" 的主任务应该是上架，而不是分数略高的改标题。
        primary = best_intent
        if strong[1][1][0] >= strong[0][1][0] * COMPOSITE_TIE_RATIO:
            primary = min(strong[:2], key=lambda item: item[1][1])[0]
        if primary is not best_intent:
            spec = INTENT_SPECS[primary]
            slots = _extract_slots(body, trimmed, spec)
        confidence = round(_base_confidence(strong[0][1][0]) * 0.9, 2)
        return IntentResult(
            intent=primary,
            confidence=confidence,
            slots=slots,
            missing=_missing_slots(spec, slots, defaults),
            candidates=[intent.value for intent, _detail in strong if intent is not primary][:2],
            raw_text=raw,
        )

    # 2.5) 模糊动词 + 文案/素材两个部位并列：不是听不懂，是一次点了两件不同的活。
    #      按分数高的那个（素材 2.5）直接派单，等于替用户决定"先做图"。
    #      靠降权重解决不了：单说"弄图片"仍然该判 MEDIA，区别只在有没有第二个部位。
    #      必须放在复合判定之后——"把主图重做，同时优化标题"这类有连接词的，
    #      复合分支已经按"先说的先做"定了主任务，不能被这里抢走。
    if _VAGUE_TASK.search(body) and _COPY_PART.search(body) and _MEDIA_PART.search(body):
        return IntentResult(
            intent=Intent.CLARIFY,
            confidence=round(min(CONFIDENCE_FLOOR - 0.01, _base_confidence(best_score)), 2),
            slots=slots,
            candidates=[intent.value for intent, _detail in ranked[:2]],
            raw_text=raw,
        )

    # 3) 歧义：前两名咬得太紧，不猜。
    if second_score >= best_score * AMBIGUITY_RATIO:
        return IntentResult(
            intent=Intent.CLARIFY,
            confidence=round(min(CONFIDENCE_FLOOR - 0.01, _base_confidence(best_score)), 2),
            slots=slots,
            candidates=[ranked[0][0].value, ranked[1][0].value],
            raw_text=raw,
        )

    # 4) 正常判定：按与次优的差距打折，差距越小越没底。
    gap = (best_score - second_score) / best_score
    confidence = round(_base_confidence(best_score) * (0.70 + 0.30 * gap), 2)

    if confidence < CONFIDENCE_FLOOR:
        # 分差够了但绝对分低（只有一个弱动词），仍然要问一句。
        return IntentResult(
            intent=Intent.CLARIFY,
            confidence=confidence,
            slots=slots,
            candidates=[intent.value for intent, _detail in ranked[:2]],
            raw_text=raw,
        )

    return IntentResult(
        intent=best_intent,
        confidence=confidence,
        slots=slots,
        missing=_missing_slots(spec, slots, defaults),
        candidates=[],
        raw_text=raw,
    )
