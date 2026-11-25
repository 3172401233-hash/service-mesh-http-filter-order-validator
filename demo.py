"""离线演示：复现“认证依赖原始 Host、限流读到改写后 Path”的顺序问题。

运行：python3 demo.py
全程纯内存模拟，不会发起任何网络连接。
"""

from filter_order import (
    Condition,
    FilterEngine,
    FilterSpec,
    LocalReply,
    Modification,
    Request,
    detect_conflicts,
)


def build_filters(rewrite_first):
    rewrite_order = 1 if rewrite_first else 3
    limit_order = 3 if rewrite_first else 1
    return [
        FilterSpec(
            name="path-rewriter",
            order=rewrite_order,
            match=[Condition("path", "prefix", "/api")],
            modifies=[Modification("path", prefix="/internal")],
        ),
        FilterSpec(
            name="rate-limit",
            order=limit_order,
            match=[Condition("path", "prefix", "/api")],
            local_reply=LocalReply(429, "rate limited"),
            depends_on=["path"],
        ),
        FilterSpec(
            name="auth",
            order=2,
            match=[
                Condition("host", "eq", "api.example.com"),
                Condition("header.authorization", "absent"),
            ],
            local_reply=LocalReply(401, "unauthorized"),
            depends_on=["host"],
        ),
    ]


def print_result(title, engine, request):
    result = engine.evaluate(request)
    print("=" * 60)
    print(title)
    print("执行链：")
    for entry in result.chain:
        flag = "命中" if entry.matched else "未命中"
        print("  #%d %-14s [%s] action=%s %s" % (
            entry.index, entry.name, flag, entry.action, entry.note
        ))
    print("最终请求：method=%s host=%s path=%s headers=%s" % (
        result.final_request.method,
        result.final_request.host,
        result.final_request.path,
        result.final_request.headers,
    ))
    print("最终响应：%d (%s)" % (result.response.status, result.response.source))
    if result.suppressed_rejections:
        print("被抑制的拒绝：%s" % ", ".join(result.suppressed_rejections))
    for impact in result.order_impacts:
        print("顺序影响：%s" % impact)


def main():
    request = Request(
        method="GET",
        host="api.example.com",
        path="/api/orders",
        headers={"authorization": "Bearer token"},
    )

    bad_engine = FilterEngine(build_filters(rewrite_first=True), [])
    print_result("场景 A：path-rewriter 排在 rate-limit 之前（错误顺序）",
                 bad_engine, request)

    good_engine = FilterEngine(build_filters(rewrite_first=False), [])
    print_result("场景 B：rate-limit 排在 path-rewriter 之前（正确顺序）",
                 good_engine, request)

    print("=" * 60)
    print("静态顺序冲突分析（场景 A 配置）：")
    for conflict in detect_conflicts(build_filters(rewrite_first=True)):
        print("  [%s/%s] %s" % (conflict.severity, conflict.kind, conflict.message))


if __name__ == "__main__":
    main()
