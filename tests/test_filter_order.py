import unittest

from filter_order import (
    Condition,
    FilterEngine,
    FilterSpec,
    LocalReply,
    Modification,
    Request,
    detect_conflicts,
)
from filter_order.conflicts import (
    CONFLICT_ORDER,
    CONFLICT_REJECTION,
    CONFLICT_STALE,
)
from filter_order.engine import build_chain
from filter_order.models import PHASE_DECODE, PHASE_ENCODE


def make_request(path="/api/foo", host="api.example.com", headers=None):
    return Request(method="GET", host=host, path=path, headers=headers or {})


class TestBasicChain(unittest.TestCase):
    def test_passthrough_chain_order_and_upstream_response(self):
        specs = [
            FilterSpec(name="f1", match=[], order=1),
            FilterSpec(name="f2", match=[], order=2),
        ]
        engine = FilterEngine(specs, [])
        result = engine.evaluate(make_request())

        self.assertEqual([entry.name for entry in result.chain], ["f1", "f2"])
        self.assertTrue(all(entry.matched for entry in result.chain))
        self.assertEqual(result.response.status, 200)
        self.assertEqual(result.response.source, "upstream")
        self.assertFalse(result.rejected)

    def test_default_order_follows_configuration_sequence(self):
        specs = [
            FilterSpec(name="later"),
            FilterSpec(name="earlier"),
        ]
        chain = build_chain(specs, [], PHASE_DECODE)
        self.assertEqual([node.spec.name for node in chain], ["later", "earlier"])


class TestHeaderRewrite(unittest.TestCase):
    def test_header_rewrite_changes_later_match(self):
        rewriter = FilterSpec(
            name="rewrite-tenant",
            order=1,
            match=[Condition("path", "eq", "/x")],
            modifies=[Modification("header.x-tenant", set="rewritten")],
        )
        guard = FilterSpec(
            name="tenant-guard",
            order=2,
            match=[Condition("header.x-tenant", "eq", "original")],
            local_reply=LocalReply(403, "forbidden"),
            depends_on=["header.x-tenant"],
        )
        engine = FilterEngine([rewriter, guard], [])
        result = engine.evaluate(
            make_request(path="/x", headers={"x-tenant": "original"})
        )

        self.assertEqual(result.final_request.headers["x-tenant"], "rewritten")
        self.assertEqual(result.chain[1].action, "pass")
        self.assertFalse(result.chain[1].matched)
        self.assertIn("tenant-guard", result.suppressed_rejections)
        self.assertTrue(any("错误放行" in note for note in result.order_impacts))
        self.assertEqual(result.response.status, 200)

    def test_guard_before_rewriter_rejects_first(self):
        guard = FilterSpec(
            name="tenant-guard",
            order=1,
            match=[Condition("header.x-tenant", "eq", "original")],
            local_reply=LocalReply(403),
        )
        rewriter = FilterSpec(
            name="rewrite-tenant",
            order=2,
            match=[],
            modifies=[Modification("header.x-tenant", set="rewritten")],
        )
        result = FilterEngine([guard, rewriter], []).evaluate(
            make_request(headers={"x-tenant": "original"})
        )
        self.assertEqual(result.response.status, 403)
        self.assertEqual([entry.name for entry in result.chain], ["tenant-guard"])

    def test_path_prefix_and_suffix_modification(self):
        spec = FilterSpec(
            name="path-edit",
            match=[],
            modifies=[
                Modification("path", prefix="/internal"),
                Modification("path", suffix="/tail"),
            ],
        )
        result = FilterEngine([spec], []).evaluate(make_request(path="/api"))
        self.assertEqual(result.final_request.path, "/internal/api/tail")
        self.assertEqual(result.changes[0].before, "/api")


class TestLocalReply(unittest.TestCase):
    def test_local_reply_terminates_chain(self):
        first = FilterSpec(
            name="blocker",
            order=1,
            match=[],
            local_reply=LocalReply(401, "unauth", {"x-block": "1"}),
        )
        second = FilterSpec(name="never-runs", order=2, match=[])
        result = FilterEngine([first, second], []).evaluate(make_request())

        self.assertTrue(result.rejected)
        self.assertEqual(result.response.status, 401)
        self.assertEqual(result.response.body, "unauth")
        self.assertEqual(result.response.headers["x-block"], "1")
        self.assertEqual([entry.name for entry in result.chain], ["blocker"])

    def test_multiple_filters_reject_first_one_wins(self):
        rate_limit = FilterSpec(
            name="rate-limit",
            order=1,
            match=[Condition("path", "prefix", "/api")],
            local_reply=LocalReply(429),
        )
        auth = FilterSpec(
            name="auth",
            order=2,
            match=[Condition("host", "eq", "api.example.com")],
            local_reply=LocalReply(403),
        )
        result = FilterEngine([rate_limit, auth], []).evaluate(make_request())

        self.assertEqual(result.response.status, 429)
        self.assertEqual(result.response.source, "local_reply")
        self.assertEqual(result.suppressed_rejections, ["auth"])


class TestRouteOverride(unittest.TestCase):
    def _listener_auth(self):
        return FilterSpec(
            name="auth",
            scope="listener",
            order=1,
            match=[Condition("host", "eq", "api.example.com")],
            local_reply=LocalReply(401),
            depends_on=["host"],
        )

    def test_route_filter_overrides_listener_same_name(self):
        route_auth = FilterSpec(
            name="auth",
            scope="route",
            order=1,
            match=[Condition("path", "prefix", "/health")],
        )
        engine = FilterEngine([self._listener_auth()], [route_auth])
        chain = build_chain([self._listener_auth()], [route_auth])

        self.assertEqual(len(chain), 1)
        self.assertEqual(chain[0].spec.scope, "route")

        result = engine.evaluate(make_request(path="/health"))
        self.assertEqual(result.response.status, 200)
        self.assertEqual(result.chain[0].name, "auth")
        self.assertEqual(result.chain[0].scope, "route")

    def test_route_can_disable_listener_filter(self):
        disabled = FilterSpec(name="auth", scope="route", disabled=True)
        chain = build_chain([self._listener_auth()], [disabled])
        self.assertEqual(chain, [])

        result = FilterEngine([self._listener_auth()], [disabled]).evaluate(
            make_request()
        )
        self.assertEqual(result.chain, [])
        self.assertEqual(result.response.status, 200)

    def test_route_adds_extra_filter_after_listener_chain(self):
        listener = FilterSpec(name="listener-only", order=1, match=[])
        route_extra = FilterSpec(name="route-only", scope="route", match=[])
        chain = build_chain([listener], [route_extra])
        self.assertEqual(
            [node.spec.name for node in chain], ["listener-only", "route-only"]
        )


class TestWrongAllowScenario(unittest.TestCase):
    """复现题目场景：认证依赖原始 Host，限流读取被改写后的 Path。"""

    def _filters(self, bad_order):
        path_rewriter = FilterSpec(
            name="path-rewriter",
            order=1 if bad_order else 2,
            match=[Condition("path", "prefix", "/api")],
            modifies=[Modification("path", prefix="/internal")],
        )
        rate_limit = FilterSpec(
            name="rate-limit",
            order=2 if bad_order else 1,
            match=[Condition("path", "prefix", "/api")],
            local_reply=LocalReply(429, "rate limited"),
            depends_on=["path"],
        )
        auth = FilterSpec(
            name="auth",
            order=3,
            match=[
                Condition("host", "eq", "api.example.com"),
                Condition("header.authorization", "absent"),
            ],
            local_reply=LocalReply(401),
            depends_on=["host"],
        )
        return [path_rewriter, rate_limit, auth]

    def test_bad_order_wrongly_allows_rate_limited_request(self):
        engine = FilterEngine(self._filters(bad_order=True), [])
        result = engine.evaluate(
            make_request(headers={"authorization": "Bearer token"})
        )

        self.assertEqual(result.final_request.path, "/internal/api/foo")
        self.assertEqual(result.response.status, 200)
        self.assertIn("rate-limit", result.suppressed_rejections)
        rate_entry = next(entry for entry in result.chain if entry.name == "rate-limit")
        self.assertFalse(rate_entry.matched)
        self.assertIn("被跳过", rate_entry.note)

    def test_bad_order_auth_still_sees_original_host(self):
        engine = FilterEngine(self._filters(bad_order=True), [])
        result = engine.evaluate(make_request())  # 无 authorization
        self.assertEqual(result.response.status, 401)
        self.assertEqual(result.response.source, "local_reply")

    def test_correct_order_rejects_with_429(self):
        engine = FilterEngine(self._filters(bad_order=False), [])
        result = engine.evaluate(
            make_request(headers={"authorization": "Bearer token"})
        )
        self.assertEqual(result.response.status, 429)
        self.assertEqual([entry.name for entry in result.chain], ["rate-limit"])


class TestConflictDetection(unittest.TestCase):
    def test_stale_read_conflict_when_dependency_is_rewritten(self):
        writer = FilterSpec(
            name="path-rewriter",
            order=1,
            modifies=[Modification("path", prefix="/internal")],
        )
        reader = FilterSpec(
            name="rate-limit",
            order=2,
            match=[Condition("path", "prefix", "/api")],
            depends_on=["path"],
            local_reply=LocalReply(429),
        )
        conflicts = detect_conflicts([writer, reader])
        stale = [conflict for conflict in conflicts if conflict.kind == CONFLICT_STALE]
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0].writer, "path-rewriter")
        self.assertEqual(stale[0].reader, "rate-limit")
        self.assertEqual(stale[0].field, "path")

    def test_order_dependency_conflict_for_match_only_reader(self):
        writer = FilterSpec(
            name="rewriter", order=1, modifies=[Modification("host", set="h2")]
        )
        reader = FilterSpec(
            name="matcher", order=2, match=[Condition("host", "eq", "h1")]
        )
        conflicts = detect_conflicts([writer, reader])
        self.assertEqual([conflict.kind for conflict in conflicts], [CONFLICT_ORDER])

    def test_no_conflict_when_reader_runs_before_writer(self):
        reader = FilterSpec(
            name="rate-limit",
            order=1,
            match=[Condition("path", "prefix", "/api")],
            depends_on=["path"],
        )
        writer = FilterSpec(
            name="path-rewriter",
            order=2,
            modifies=[Modification("path", prefix="/internal")],
        )
        self.assertEqual(detect_conflicts([reader, writer]), [])

    def test_multiple_rejection_conflict(self):
        first = FilterSpec(
            name="f1",
            order=1,
            match=[Condition("path", "prefix", "/api")],
            local_reply=LocalReply(429),
        )
        second = FilterSpec(
            name="f2",
            order=2,
            match=[Condition("host", "eq", "api.example.com")],
            local_reply=LocalReply(403),
        )
        conflicts = detect_conflicts([first, second])
        kinds = {conflict.kind for conflict in conflicts}
        self.assertIn(CONFLICT_REJECTION, kinds)

    def test_disjoint_match_does_not_report_rejection_conflict(self):
        first = FilterSpec(
            name="f1",
            order=1,
            match=[Condition("path", "eq", "/a")],
            local_reply=LocalReply(429),
        )
        second = FilterSpec(
            name="f2",
            order=2,
            match=[Condition("path", "eq", "/b")],
            local_reply=LocalReply(403),
        )
        conflicts = detect_conflicts([first, second])
        self.assertNotIn(CONFLICT_REJECTION, {item.kind for item in conflicts})


class TestEncodePhase(unittest.TestCase):
    def test_encode_filter_runs_in_reverse_and_edits_response(self):
        decoder = FilterSpec(
            name="decoder", phase=PHASE_DECODE, order=1, match=[]
        )
        encoder = FilterSpec(
            name="encoder",
            phase=PHASE_ENCODE,
            match=[Condition("status", "eq", "200")],
            modifies=[Modification("header.x-resp", set="seen")],
        )
        result = FilterEngine([decoder, encoder], []).evaluate(make_request())
        self.assertEqual(result.response.headers["x-resp"], "seen")
        encode_entries = [e for e in result.chain if e.phase == PHASE_ENCODE]
        self.assertEqual(encode_entries[0].name, "encoder")
        self.assertTrue(encode_entries[0].matched)


class TestDictConfig(unittest.TestCase):
    def test_from_dicts_end_to_end(self):
        listener = [
            {
                "name": "auth",
                "order": 1,
                "match": [
                    {"field": "host", "operator": "eq", "value": "api.example.com"},
                    {"field": "header.authorization", "operator": "absent"},
                ],
                "local_reply": {"status": 401, "body": "unauthorized"},
                "depends_on": ["host"],
            }
        ]
        engine = FilterEngine.from_dicts(listener, [])
        result = engine.evaluate(Request.from_dict({
            "host": "api.example.com",
            "path": "/api/foo",
            "headers": {},
        }))
        self.assertEqual(result.response.status, 401)
        self.assertEqual(result.response.body, "unauthorized")


if __name__ == "__main__":
    unittest.main()
