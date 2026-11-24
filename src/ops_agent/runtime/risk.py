"""工具风险分级 R0—R3。

这一层只回答一个问题：**这个工具最高算几级**。它不做过滤，也不碰调用链——
过滤在 `runtime/gateway.py`，调用在 `tools.py`。分开的理由是同一个等级表要被
三处读（注册时写进 spec、网关按 policy 过滤、测试做快照），放在一处才不会漂。

| 等级 | 定义 | 推导规则 | 模型可见 | 人工确认 |
| --- | --- | --- | --- | --- |
| R0 | 只读 / 计算 | `side_effect in {read, compute}` | 是 | 否 |
| R1 | 单条草稿写入 | `write` 且 `requires_approval` | 是 | 是 |
| R2 | 批量 / 无人守的写入 | `write` 且不要求审批 | 是 | 是 + 抽样 |
| R3 | 不可逆 | 工具名在 `FORBIDDEN_TOOLS` 内 | **永远否** | 走一次性令牌 |

两条恒等式，后面几步都依赖：

1. **R3 不看配置。** 名字在 `FORBIDDEN_TOOLS` 里就是 R3，`max_risk` 取什么值
   都不可见。覆盖表也不能把它降回来。
2. **未知即失败关闭。** 认不出的 `side_effect` 一律按 R3 处理——宁可少暴露一个
   工具，也不要因为拼写错误把一个写工具当只读放出去。
"""

from __future__ import annotations

from ..agents.base import FORBIDDEN_TOOLS

# 顺序即强弱，索引就是秩。不要在中间插入新等级：`max_risk` 的比较是纯字符串
# 之外唯一依赖位置的地方，插进去会让所有既有快照测试失去意义。
LEVELS: tuple[str, ...] = ("R0", "R1", "R2", "R3")

_RANK: dict[str, int] = {level: index for index, level in enumerate(LEVELS)}

# 只读类副作用。`compute` 也算只读：它产出草稿但从不落库。
_READ_ONLY = frozenset({"read", "compute"})

# 覆盖表：默认规则说不清、或者必须钉死的工具写在这里。
# `publish_listing` / `rollback` 同时也出现在 FORBIDDEN_TOOLS 里，两条路径都指向
# R3，是为了让"有人只改一处"这种事故也能被另一处挡住。
OVERRIDES: dict[str, str] = {
    "publish_listing": "R3",
    "rollback": "R3",
}


def rank(level: str) -> int:
    """等级 → 序号；认不出的等级直接报错，不要静默当 R0。"""

    try:
        return _RANK[str(level).strip().upper()]
    except KeyError:
        raise ValueError(f"未知风险等级: {level!r}（只支持 {', '.join(LEVELS)}）") from None


def check_level(level: str) -> str:
    """规范化并校验一个等级字符串，供 CLI / 测试复用。"""

    text = str(level).strip().upper()
    rank(text)
    return text


def infer_risk_level(name: str, side_effect: str, requires_approval: bool) -> str:
    """按名字 + 副作用 + 审批要求推导等级。

    `name` 优先：不可逆动作即使被误标成 `read`，也必须还是 R3。
    """

    tool = str(name or "").strip()
    if tool in FORBIDDEN_TOOLS:
        return "R3"
    override = OVERRIDES.get(tool)
    if override:
        return check_level(override)

    effect = str(side_effect or "").strip().lower()
    if effect in _READ_ONLY:
        return "R0"
    if effect == "write":
        return "R1" if requires_approval else "R2"
    # 认不出的副作用：失败关闭。
    return "R3"


def is_visible(level: str, max_risk: str | None) -> bool:
    """`max_risk=None` 表示"不做等级限制"，但 R3 永远不可见。"""

    current = check_level(level)
    if current == "R3":
        return False
    if max_risk is None:
        return True
    return rank(current) <= rank(check_level(max_risk))
