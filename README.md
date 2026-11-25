# service-mesh-http-filter-order-validator

验证服务网格（Envoy 风格）HTTP filter 执行顺序的**离线模拟器**：输入 listener
与 route 两级过滤器配置及请求，计算每个请求实际经过的过滤器链、最终请求与
最终响应，并静态分析顺序冲突。

## 重要说明：不连接真实代理

本工具只在本地用 Python 对象模拟 filter 语义，**不会启动 Envoy/sidecar，
不会监听端口，也不会发起任何网络请求或 DNS 查询**。它用于在评审配置时
提前发现顺序问题，不能替代真实代理上的集成测试。

## 适用场景

同一条路由上同时配置认证、请求改写、限流、故障注入等过滤器时，回答：

- route 级过滤器如何覆盖 listener 级同名过滤器（含显式禁用）；
- 某个过滤器命中后提前 `local_reply`，链上哪些过滤器不会再执行；
- 字段（`host`/`path`/`method`/`header.*`）被改写后，后续过滤器的
  `match` 与声明依赖读到的是新值还是旧值；
- 多个过滤器同时满足拒绝条件时，谁先生效、谁的拒绝被静默抑制；
- 交换两个过滤器的顺序，请求结果是否会变化。

## 环境

- Python 3.8.2+（仅使用标准库 `dataclasses`、`typing`、`unittest`，
  无第三方依赖；未使用 3.10+ 的联合类型语法或 3.10+ 的 `match` 语句）。

## 数据模型

过滤器 `FilterSpec` 的输入字段：

| 字段 | 含义 |
| --- | --- |
| `name` | 过滤器唯一名称，route 同名过滤器据此覆盖 listener 过滤器 |
| `scope` | `listener`（默认）或 `route` |
| `phase` | `decode`（请求方向，默认）或 `encode`（响应方向，反向执行） |
| `order` | 链上顺序值，越小越靠前；缺省按配置出现顺序 |
| `disabled` | route 用 `true` 显式关闭 listener 同名过滤器 |
| `match` | 条件列表（AND），支持 `eq/neq/prefix/suffix/contains/present/absent` |
| `modifies` | 改写动作，支持 `set/prefix/suffix/remove` |
| `local_reply` | 命中即本地返回 `{status, body, headers}` 并终止 decode 链 |
| `depends_on` | 声明依赖原始值的字段，用于顺序冲突分析 |

字段名：`host`、`path`、`method`、`status`（encode 阶段）以及
`header.<Name>`（header 名大小写不敏感）。

## 快速使用

```python
from filter_order import FilterEngine, Request

engine = FilterEngine.from_dicts(
    listener_filters=[
        {
            "name": "path-rewriter",
            "order": 1,
            "match": [{"field": "path", "operator": "prefix", "value": "/api"}],
            "modifies": [{"field": "path", "prefix": "/internal"}],
        },
        {
            "name": "rate-limit",
            "order": 2,
            "match": [{"field": "path", "operator": "prefix", "value": "/api"}],
            "local_reply": {"status": 429, "body": "rate limited"},
            "depends_on": ["path"],
        },
    ],
    route_filters=[],
)

result = engine.evaluate(Request(host="api.example.com", path="/api/orders"))
print(result.final_request.path)        # /internal/api/orders
print(result.response.status)           # 200，限流被错误放行
print(result.suppressed_rejections)     # ['rate-limit']
```

也可以直接运行演示脚本（纯本地输出，无网络）：

```bash
python3 demo.py
```

## 顺序冲突类型

`detect_conflicts(listener_filters, route_filters)` 对合并后的链做静态分析：

- `stale_read`（error）：上游改写了某字段，下游通过 `depends_on` 声明依赖
  该字段的原始值 —— 典型的“认证看原始 Host、限流读改写后 Path”问题；
- `order_dependency`（warning）：下游 `match` 读取了上游会改写的字段，
  交换顺序即改变命中结果；
- `multiple_rejection`（warning）：两个带 `local_reply` 的过滤器可能同时
  命中，链上靠前的先生效，靠后的拒绝被抑制。

运行时结果 `RequestResult` 另外提供：

- `chain`：每个过滤器的命中情况、动作与改写记录；
- `changes`：字段改写前后值及改写者；
- `order_impacts`：本可命中原始请求、却因改写而跳过的过滤器说明；
- `suppressed_rejections`：被抑制的本地返回过滤器名称。

## 运行测试

```bash
python -m unittest
```

测试覆盖：header/path 改写影响后续规则、`local_reply` 提前终止链与多重
拒绝、route 覆盖/禁用 listener 过滤器、错误顺序导致请求被错误放行，以及
三类顺序冲突的静态检测。
