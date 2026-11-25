"""过滤器链构建与请求执行引擎（纯内存模拟）。"""

import copy
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .models import (
    ChainEntry,
    Condition,
    FieldChange,
    FilterSpec,
    LocalReply,
    Modification,
    PHASE_DECODE,
    PHASE_ENCODE,
    Request,
    RequestResult,
    Response,
)


def _header_name(field: str) -> Optional[str]:
    """header.xxx -> 小写 header 名；非 header 字段返回 None。"""
    if field.startswith("header."):
        return field[len("header."):].lower()
    return None


def get_field(holder: Any, field: str) -> Optional[str]:
    """从请求/响应上读取规范字段，不存在返回 None。"""
    header = _header_name(field)
    if header is not None:
        return holder.headers.get(header)
    if field == "host":
        return holder.host
    if field == "path":
        return holder.path
    if field == "method":
        return holder.method
    if field == "status" and isinstance(holder, Response):
        return str(holder.status)
    raise ValueError("无法读取字段 %r" % field)


def set_field(holder: Any, field: str, value: Optional[str]) -> None:
    """写入规范字段；header 传 None 表示删除该 header。"""
    header = _header_name(field)
    if header is not None:
        if value is None:
            holder.headers.pop(header, None)
        else:
            holder.headers[header] = value
        return
    if field == "host":
        holder.host = value if value is not None else ""
        return
    if field == "path":
        holder.path = value if value is not None else "/"
        return
    if field == "method":
        holder.method = value if value is not None else ""
        return
    raise ValueError("无法写入字段 %r" % field)


def condition_matches(holder: Any, condition: Condition) -> bool:
    actual = get_field(holder, condition.field)
    operator = condition.operator
    if operator == "present":
        return actual is not None
    if operator == "absent":
        return actual is None
    if actual is None:
        return False
    expected = condition.value
    if operator == "eq":
        return actual == expected
    if operator == "neq":
        return actual != expected
    if operator == "prefix":
        return actual.startswith(expected or "")
    if operator == "suffix":
        return actual.endswith(expected or "")
    if operator == "contains":
        return (expected or "") in actual
    raise ValueError("未知操作符 %r" % operator)


def all_conditions_match(holder: Any, conditions: Sequence[Condition]) -> bool:
    return all(condition_matches(holder, condition) for condition in conditions)


@dataclass
class ChainNode:
    """链上的一个有效过滤器（已完成 route 覆盖 listener 的合并）。"""

    spec: FilterSpec
    origin_index: int


def build_chain(
    listener_filters: Sequence[FilterSpec],
    route_filters: Optional[Sequence[FilterSpec]] = None,
    phase: str = PHASE_DECODE,
) -> List[ChainNode]:
    """合并 listener 与 route 级过滤器。

    规则（模拟 Envoy 风格的 route 级 filter 覆盖）：
    1. 只保留指定 phase 的过滤器；
    2. route 上同名过滤器覆盖 listener 同名过滤器（含 disabled=true 的禁用场景）；
    3. 先按 order 升序，order 缺失时按配置出现顺序（listener 在前、route 在后）。
    """
    route_filters = list(route_filters or [])
    route_by_name = {}  # type: Dict[str, FilterSpec]
    for spec in route_filters:
        if spec.phase != phase:
            continue
        route_by_name[spec.name] = spec

    merged = []  # type: List[Tuple[FilterSpec, int]]
    origin = 0
    for spec in listener_filters:
        if spec.phase != phase:
            continue
        override = route_by_name.pop(spec.name, None)
        if override is not None:
            if not override.disabled:
                merged.append((override, origin))
        elif not spec.disabled:
            merged.append((spec, origin))
        origin += 1

    for spec in route_filters:
        if spec.phase != phase or spec.name not in route_by_name:
            continue
        if not spec.disabled:
            merged.append((spec, origin))
        origin += 1

    merged.sort(key=lambda item: (
        item[0].order if item[0].order is not None else item[1],
        item[1],
    ))
    return [ChainNode(spec=spec, origin_index=index) for spec, index in merged]


def _apply_modification(
    holder: Request, modification: Modification, filter_name: str
) -> FieldChange:
    before = get_field(holder, modification.field)
    if modification.remove:
        after = None  # type: Optional[str]
    elif modification.set is not None:
        after = modification.set
    else:
        current = before or ""
        after = "%s%s%s" % (
            modification.prefix or "",
            current,
            modification.suffix or "",
        )
    set_field(holder, modification.field, after)
    return FieldChange(
        field=modification.field, before=before, after=after, by_filter=filter_name
    )


def _original_request_matches(spec: FilterSpec, original: Request) -> bool:
    return all_conditions_match(original, spec.match)


class FilterEngine:
    """根据 listener/route 配置计算每个请求的过滤器链与最终响应。"""

    def __init__(
        self,
        listener_filters: Sequence[FilterSpec],
        route_filters: Optional[Sequence[FilterSpec]] = None,
        upstream_status: int = 200,
    ) -> None:
        self.listener_filters = list(listener_filters)
        self.route_filters = list(route_filters or [])
        self.upstream_status = upstream_status
        self.decode_chain = build_chain(
            self.listener_filters, self.route_filters, PHASE_DECODE
        )
        self.encode_chain = build_chain(
            self.listener_filters, self.route_filters, PHASE_ENCODE
        )

    @classmethod
    def from_dicts(
        cls,
        listener_filters: Sequence[Dict[str, Any]],
        route_filters: Optional[Sequence[Dict[str, Any]]] = None,
        upstream_status: int = 200,
    ) -> "FilterEngine":
        return cls(
            [FilterSpec.from_dict(item) for item in listener_filters],
            [FilterSpec.from_dict(item) for item in route_filters or []],
            upstream_status=upstream_status,
        )

    def evaluate(self, request: Request) -> RequestResult:
        original = copy.deepcopy(request)
        working = copy.deepcopy(request)
        chain = []  # type: List[ChainEntry]
        changes = []  # type: List[FieldChange]
        order_impacts = []  # type: List[str]
        suppressed_rejections = []  # type: List[str]
        local_reply = None  # type: Optional[LocalReply]

        for position, node in enumerate(self.decode_chain):
            spec = node.spec
            matched = all_conditions_match(working, spec.match)
            entry = ChainEntry(
                index=position,
                name=spec.name,
                scope=spec.scope,
                phase=spec.phase,
                matched=matched,
            )

            if not matched:
                if spec.match and _original_request_matches(spec, original):
                    note = (
                        "过滤器 %s 本可命中原始请求，但依赖字段已被前面的过滤器改写，"
                        "当前被跳过" % spec.name
                    )
                    entry.note = note
                    order_impacts.append(note)
                    if spec.local_reply is not None:
                        suppressed_rejections.append(spec.name)
                        order_impacts.append(
                            "过滤器 %s 的本地返回因改写后不再命中而被抑制（请求被错误放行）"
                            % spec.name
                        )
                chain.append(entry)
                continue

            if spec.modifies:
                for modification in spec.modifies:
                    change = _apply_modification(working, modification, spec.name)
                    entry.changes.append(change)
                    changes.append(change)
                entry.action = "rewrite"

            if spec.local_reply is not None:
                local_reply = spec.local_reply
                entry.action = "local_reply"
                entry.note = "命中本地返回，终止后续 decode 过滤器"
                chain.append(entry)
                for later in self.decode_chain[position + 1:]:
                    later_spec = later.spec
                    if later_spec.local_reply is not None and all_conditions_match(
                        working, later_spec.match
                    ):
                        suppressed_rejections.append(later_spec.name)
                break

            chain.append(entry)

        if local_reply is not None:
            response = Response(
                status=local_reply.status,
                body=local_reply.body,
                headers=dict(local_reply.headers),
                source="local_reply",
            )
        else:
            response = Response(status=self.upstream_status, source="upstream")
            response = self._run_encode(working, response, chain)

        return RequestResult(
            request=original,
            final_request=working,
            response=response,
            chain=chain,
            changes=changes,
            order_impacts=order_impacts,
            suppressed_rejections=suppressed_rejections,
        )

    def _run_encode(
        self, request: Request, response: Response, chain: List[ChainEntry]
    ) -> Response:
        # encode 阶段按 decode 的反方向执行（响应回程）
        for position, node in enumerate(reversed(self.encode_chain)):
            spec = node.spec
            matched = all_conditions_match(response, spec.match)
            entry = ChainEntry(
                index=len(self.decode_chain) + position,
                name=spec.name,
                scope=spec.scope,
                phase=spec.phase,
                matched=matched,
            )
            if not matched:
                chain.append(entry)
                continue
            if spec.modifies:
                for modification in spec.modifies:
                    change = _apply_modification(response, modification, spec.name)
                    entry.changes.append(change)
                entry.action = "rewrite"
            if spec.local_reply is not None:
                reply = spec.local_reply
                response = Response(
                    status=reply.status,
                    body=reply.body,
                    headers=dict(reply.headers),
                    source="local_reply",
                )
                entry.action = "local_reply"
                entry.note = "encode 阶段命中本地返回"
                chain.append(entry)
                break
            chain.append(entry)
        return response


def evaluate(
    listener_filters: Sequence[FilterSpec],
    route_filters: Sequence[FilterSpec],
    requests: Sequence[Request],
) -> List[RequestResult]:
    """便捷入口：批量执行一组请求。"""
    engine = FilterEngine(listener_filters, route_filters)
    return [engine.evaluate(request) for request in requests]
