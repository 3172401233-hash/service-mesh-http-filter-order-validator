"""过滤器与请求/响应的数据模型。

全部使用 Python 3.8 兼容语法（dataclass + typing，不使用 PEP 604/634）。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 作用域
SCOPE_LISTENER = "listener"
SCOPE_ROUTE = "route"

# 阶段
PHASE_DECODE = "decode"   # 请求进入方向
PHASE_ENCODE = "encode"   # 响应返回方向

# 条件支持的操作符
OPERATORS = ("eq", "neq", "prefix", "suffix", "contains", "present", "absent")

# 可被匹配/改写的规范字段
FIELD_HOST = "host"
FIELD_PATH = "path"
FIELD_METHOD = "method"


def _as_dict(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    return dict(value)


@dataclass
class Condition:
    """match 条件：对某个字段做操作符比较。

    字段名支持 host/path/method 以及 header.<Name>（header 名大小写不敏感）。
    """

    field: str
    operator: str
    value: Optional[str] = None

    def __post_init__(self) -> None:
        if self.operator not in OPERATORS:
            raise ValueError(
                "不支持的操作符 %r，可选：%s" % (self.operator, ", ".join(OPERATORS))
            )
        if self.operator not in ("present", "absent") and self.value is None:
            raise ValueError("操作符 %r 需要提供 value" % self.operator)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Condition":
        return cls(
            field=data["field"],
            operator=data.get("operator", "eq"),
            value=data.get("value"),
        )


@dataclass
class Modification:
    """请求改写动作。

    set/prefix/suffix/remove 分别作用于 host、path、method 或 header.<Name>。
    """

    field: str
    set: Optional[str] = None
    prefix: Optional[str] = None
    suffix: Optional[str] = None
    remove: bool = False

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Modification":
        return cls(
            field=data["field"],
            set=data.get("set"),
            prefix=data.get("prefix"),
            suffix=data.get("suffix"),
            remove=bool(data.get("remove", False)),
        )


@dataclass
class LocalReply:
    """过滤器提前本地返回（直接回复，不再继续过滤器链）。"""

    status: int
    body: str = ""
    headers: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LocalReply":
        return cls(
            status=int(data["status"]),
            body=data.get("body", ""),
            headers=dict(_as_dict(data.get("headers"))),
        )


@dataclass
class FilterSpec:
    """一个 HTTP filter 的声明式配置。

    - name: 过滤器名（唯一标识）
    - scope: listener / route
    - phase: decode / encode（route 上的同名过滤器覆盖 listener 同名过滤器）
    - order: 同一作用域内的显式排序值，越小越靠前；缺省按配置顺序
    - disabled: route 用 disabled=true 显式关闭 listener 上的同名过滤器
    - match: 命中条件列表，空列表表示总是命中
    - modifies: 命中后执行的请求改写
    - local_reply: 命中后立即本地返回
    - depends_on: 声明依赖的字段（读取值，用于顺序冲突分析）
    """

    name: str
    scope: str = SCOPE_LISTENER
    phase: str = PHASE_DECODE
    order: Optional[int] = None
    disabled: bool = False
    match: List[Condition] = field(default_factory=list)
    modifies: List[Modification] = field(default_factory=list)
    local_reply: Optional[LocalReply] = None
    depends_on: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.scope not in (SCOPE_LISTENER, SCOPE_ROUTE):
            raise ValueError("scope 只能是 listener 或 route，收到 %r" % self.scope)
        if self.phase not in (PHASE_DECODE, PHASE_ENCODE):
            raise ValueError("phase 只能是 decode 或 encode，收到 %r" % self.phase)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FilterSpec":
        return cls(
            name=data["name"],
            scope=data.get("scope", SCOPE_LISTENER),
            phase=data.get("phase", PHASE_DECODE),
            order=data.get("order"),
            disabled=bool(data.get("disabled", False)),
            match=[Condition.from_dict(item) for item in data.get("match", [])],
            modifies=[
                Modification.from_dict(item) for item in data.get("modifies", [])
            ],
            local_reply=(
                LocalReply.from_dict(data["local_reply"])
                if data.get("local_reply")
                else None
            ),
            depends_on=list(data.get("depends_on", [])),
        )


@dataclass
class Request:
    """进入监听器的原始 HTTP 请求。"""

    method: str = "GET"
    host: str = ""
    path: str = "/"
    headers: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Request":
        return cls(
            method=data.get("method", "GET"),
            host=data.get("host", ""),
            path=data.get("path", "/"),
            headers={key: str(value) for key, value in _as_dict(data.get("headers")).items()},
        )


@dataclass
class Response:
    """最终响应：可能来自本地返回，也可能来自上游（默认 200）。"""

    status: int
    body: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    source: str = "upstream"  # upstream / local_reply


@dataclass
class FieldChange:
    """一次字段改写的前后值。"""

    field: str
    before: Optional[str]
    after: Optional[str]
    by_filter: str


@dataclass
class ChainEntry:
    """过滤器链上的一个执行记录。"""

    index: int
    name: str
    scope: str
    phase: str
    matched: bool
    action: str = "pass"          # pass / rewrite / local_reply
    note: str = ""
    changes: List[FieldChange] = field(default_factory=list)


@dataclass
class RequestResult:
    """单个请求走完过滤器链后的完整结果。"""

    request: Request
    final_request: Request
    response: Response
    chain: List[ChainEntry] = field(default_factory=list)
    changes: List[FieldChange] = field(default_factory=list)
    order_impacts: List[str] = field(default_factory=list)
    suppressed_rejections: List[str] = field(default_factory=list)

    @property
    def rejected(self) -> bool:
        return self.response.source == "local_reply"
