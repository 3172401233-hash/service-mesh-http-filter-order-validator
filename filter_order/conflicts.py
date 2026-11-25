"""过滤器顺序冲突的静态分析。

冲突类型：
1. order_dependency（顺序依赖）：下游过滤器声明依赖（depends_on 或 match 读取）
   的字段，会被上游过滤器改写。字段语义在链上不稳定，交换顺序即改变结果。
2. stale_read（过期读取）：上游已改写某字段，下游仍依赖原始语义，
   属于 order_dependency 中“读旧值”的高危子类。
3. multiple_rejection（多重拒绝）：两个带 local_reply 的过滤器 match 条件可能
   同时命中，实际只有链上靠前的生效，靠后的拒绝被静默抑制。
"""

from dataclasses import dataclass
from typing import List, Optional, Sequence, Set

from .engine import ChainNode, _header_name, build_chain
from .models import (
    Condition,
    FilterSpec,
    PHASE_DECODE,
)

CONFLICT_ORDER = "order_dependency"
CONFLICT_STALE = "stale_read"
CONFLICT_REJECTION = "multiple_rejection"
SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"


@dataclass
class Conflict:
    """一条顺序冲突记录。"""

    kind: str
    severity: str
    field: Optional[str]
    writer: str
    reader: str
    message: str


def _modified_fields(spec: FilterSpec) -> Set[str]:
    return {modification.field for modification in spec.modifies}


def _read_fields(spec: FilterSpec) -> Set[str]:
    fields = set(spec.depends_on)  # type: Set[str]
    for condition in spec.match:
        fields.add(condition.field)
    return fields


def _canonical(field: str) -> str:
    """header 名规范化为小写，便于跨过滤器比较。"""
    header = _header_name(field)
    return "header.%s" % header if header is not None else field


def _condition_overlap(left: Condition, right: Condition) -> bool:
    """两个条件是否可能同时命中（保守估计，宁可多报）。"""
    if left.field != right.field:
        return True
    if left.operator in ("present", "absent") or right.operator in ("present", "absent"):
        return True
    if left.operator != "eq" or right.operator != "eq":
        return True
    return left.value == right.value


def _match_can_overlap(left: FilterSpec, right: FilterSpec) -> bool:
    """两个过滤器的 match 条件是否可能在同一请求上同时命中。"""
    if not left.match or not right.match:
        return True
    for left_condition in left.match:
        for right_condition in right.match:
            if left_condition.field == right_condition.field:
                if not _condition_overlap(left_condition, right_condition):
                    return False
    return True


def detect_conflicts(
    listener_filters: Sequence[FilterSpec],
    route_filters: Optional[Sequence[FilterSpec]] = None,
    phase: str = PHASE_DECODE,
) -> List[Conflict]:
    """对合并后的 decode（默认）链做静态顺序冲突分析。"""
    chain = build_chain(listener_filters, route_filters, phase)
    conflicts = []  # type: List[Conflict]
    seen = set()  # type: Set[tuple]

    for upstream_index, upstream in enumerate(chain):
        written = {_canonical(name) for name in _modified_fields(upstream.spec)}
        if not written:
            continue
        for downstream in chain[upstream_index + 1:]:
            downstream_spec = downstream.spec
            for raw_field in _read_fields(downstream_spec):
                canonical = _canonical(raw_field)
                if canonical not in written:
                    continue
                key = (CONFLICT_ORDER, canonical, upstream.spec.name, downstream_spec.name)
                if key in seen:
                    continue
                seen.add(key)
                is_stale = canonical in {
                    _canonical(name) for name in downstream_spec.depends_on
                }
                kind = CONFLICT_STALE if is_stale else CONFLICT_ORDER
                severity = SEVERITY_ERROR if is_stale else SEVERITY_WARNING
                if is_stale:
                    message = (
                        "过滤器 %s 改写了 %s，而 %s 声明依赖该字段的原始值，"
                        "当前顺序下 %s 读到的是被改写后的值"
                        % (
                            upstream.spec.name,
                            raw_field,
                            downstream_spec.name,
                            downstream_spec.name,
                        )
                    )
                else:
                    message = (
                        "过滤器 %s 改写 %s 后，%s 才基于该字段做 match 判断，"
                        "交换两者顺序会改变命中结果"
                        % (upstream.spec.name, raw_field, downstream_spec.name)
                    )
                conflicts.append(
                    Conflict(
                        kind=kind,
                        severity=severity,
                        field=raw_field,
                        writer=upstream.spec.name,
                        reader=downstream_spec.name,
                        message=message,
                    )
                )

    rejectors = [node for node in chain if node.spec.local_reply is not None]
    for index, left in enumerate(rejectors):
        for right in rejectors[index + 1:]:
            if not _match_can_overlap(left.spec, right.spec):
                continue
            key = (CONFLICT_REJECTION, left.spec.name, right.spec.name)
            if key in seen:
                continue
            seen.add(key)
            conflicts.append(
                Conflict(
                    kind=CONFLICT_REJECTION,
                    severity=SEVERITY_WARNING,
                    field=None,
                    writer=left.spec.name,
                    reader=right.spec.name,
                    message=(
                        "过滤器 %s 与 %s 都可能对同一请求本地返回，%s 排在前面，"
                        "%s 的拒绝会被抑制"
                        % (left.spec.name, right.spec.name, left.spec.name, right.spec.name)
                    ),
                )
            )
    return conflicts
