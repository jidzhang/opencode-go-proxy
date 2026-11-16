"""Unit tests for the prefix-cache accounting tracker."""

import unittest

from opencode_go_proxy.cache import CacheTracker


class CacheTrackerTests(unittest.TestCase):
    def test_empty_tracker_reports_no_ratio(self) -> None:
        snap = CacheTracker().snapshot()
        self.assertEqual(snap["totals"]["hit_ratio"], None)
        self.assertEqual(snap["totals"]["requests"], 0)
        self.assertEqual(snap["models"], [])

    def test_records_hits_and_misses_per_model(self) -> None:
        tracker = CacheTracker()
        tracker.record("deepseek-v4.1-flash", hit=900, miss=10)
        tracker.record("deepseek-v4.1-flash", hit=950, miss=5)
        tracker.record("mimo-v2.5", hit=0, miss=100)

        snap = tracker.snapshot()
        self.assertEqual(len(snap["models"]), 2)

        ds = next(m for m in snap["models"] if m["model"] == "deepseek-v4.1-flash")
        self.assertEqual(ds["cache_hit_tokens"], 1850)
        self.assertEqual(ds["cache_miss_tokens"], 15)
        self.assertEqual(ds["requests"], 2)
        self.assertAlmostEqual(ds["hit_ratio"], 1850 / 1865, places=6)

        mm = next(m for m in snap["models"] if m["model"] == "mimo-v2.5")
        self.assertEqual(mm["hit_ratio"], 0.0)

        self.assertEqual(snap["totals"]["cache_hit_tokens"], 1850)
        self.assertEqual(snap["totals"]["cache_miss_tokens"], 115)
        self.assertAlmostEqual(snap["totals"]["hit_ratio"], 1850 / 1965, places=6)

    def test_negative_counts_are_ignored(self) -> None:
        tracker = CacheTracker()
        tracker.record("deepseek-v4.1-flash", hit=-1, miss=5)
        snap = tracker.snapshot()
        self.assertEqual(snap["totals"]["cache_miss_tokens"], 0)

    def test_unknown_model_records_under_unknown_key(self) -> None:
        tracker = CacheTracker()
        tracker.record(None, hit=10, miss=0)
        snap = tracker.snapshot()
        self.assertEqual(snap["models"][0]["model"], "unknown")


class CacheSessionTests(unittest.TestCase):
    def test_cold_start_then_hit_then_lost_prefix(self) -> None:
        tracker = CacheTracker()
        tracker.record("deepseek-v4.1-flash", hit=0, miss=5000, session="s1")
        tracker.record("deepseek-v4.1-flash", hit=4800, miss=200, session="s1")
        tracker.record("deepseek-v4.1-flash", hit=4900, miss=100, session="s1")
        tracker.record("deepseek-v4.1-flash", hit=0, miss=5200, session="s1")

        snap = tracker.snapshot()
        session = snap["sessions"][0]
        self.assertEqual(session["session"], "s1")
        self.assertEqual(session["requests"], 4)
        self.assertEqual(session["cold_starts"], 1)
        self.assertEqual(session["prefix_lost"], 1)
        self.assertEqual(session["cache_hit_tokens"], 9700)
        self.assertEqual(session["cache_miss_tokens"], 10500)
        self.assertEqual(snap["totals"]["sessions"], 1)
        self.assertEqual(snap["totals"]["cold_starts"], 1)
        self.assertEqual(snap["totals"]["prefix_lost"], 1)

    def test_partial_hit_is_never_counted_as_a_lost_prefix(self) -> None:
        tracker = CacheTracker()
        tracker.record("m", hit=1000, miss=100, session="s1")
        tracker.record("m", hit=1, miss=9000, session="s1")
        session = tracker.snapshot()["sessions"][0]
        self.assertEqual(session["prefix_lost"], 0)
        self.assertEqual(session["cold_starts"], 0)

    def test_sessions_are_isolated_from_each_other(self) -> None:
        tracker = CacheTracker()
        tracker.record("m", hit=900, miss=100, session="a")
        tracker.record("m", hit=0, miss=800, session="b")
        snap = tracker.snapshot()
        by_name = {row["session"]: row for row in snap["sessions"]}
        self.assertEqual(by_name["a"]["cold_starts"], 0)
        self.assertEqual(by_name["b"]["cold_starts"], 1)

    def test_sessions_are_most_recent_first(self) -> None:
        tracker = CacheTracker()
        tracker.record("m", hit=1, miss=0, session="old")
        tracker.record("m", hit=1, miss=0, session="new")
        self.assertEqual(
            [row["session"] for row in tracker.snapshot()["sessions"]],
            ["new", "old"],
        )

    def test_oldest_session_is_evicted_at_the_bound(self) -> None:
        tracker = CacheTracker(max_sessions=2)
        tracker.record("m", hit=1, miss=0, session="a")
        tracker.record("m", hit=1, miss=0, session="b")
        tracker.record("m", hit=1, miss=0, session="a")
        tracker.record("m", hit=1, miss=0, session="c")
        names = [row["session"] for row in tracker.snapshot()["sessions"]]
        self.assertEqual(names, ["c", "a"])

    def test_a_record_without_a_session_still_counts_per_model(self) -> None:
        tracker = CacheTracker()
        tracker.record("m", hit=10, miss=5)
        snap = tracker.snapshot()
        self.assertEqual(snap["sessions"], [])
        self.assertEqual(snap["totals"]["cache_hit_tokens"], 10)
        self.assertEqual(snap["totals"]["sessions"], 0)

    def test_totals_outlive_evicted_sessions(self) -> None:
        tracker = CacheTracker(max_sessions=2)
        for name in ("a", "b", "c"):
            tracker.record("m", hit=0, miss=100, session=name)
        snap = tracker.snapshot()
        # Only the two most recent sessions are listed ...
        self.assertEqual([row["session"] for row in snap["sessions"]], ["c", "b"])
        # ... but the totals still account for every session ever seen.
        self.assertEqual(snap["totals"]["sessions"], 3)
        self.assertEqual(snap["totals"]["cold_starts"], 3)


class RecordCacheTests(unittest.TestCase):
    def test_record_cache_passes_the_session_through(self) -> None:
        from opencode_go_proxy.upstream import record_cache

        tracker = CacheTracker()
        usage = {"prompt_cache_hit_tokens": 900, "prompt_cache_miss_tokens": 100}
        record_cache(tracker, "deepseek-v4.1-flash", usage, session="s1")
        session = tracker.snapshot()["sessions"][0]
        self.assertEqual(session["session"], "s1")
        self.assertEqual(session["cache_hit_tokens"], 900)

    def test_record_cache_skips_usage_without_cache_fields(self) -> None:
        from opencode_go_proxy.upstream import record_cache

        tracker = CacheTracker()
        record_cache(tracker, "m", {"prompt_tokens": 10}, session="s1")
        self.assertEqual(tracker.snapshot()["sessions"], [])

    def test_record_cache_from_body_reads_a_non_stream_usage(self) -> None:
        from opencode_go_proxy.upstream import record_cache_from_body

        tracker = CacheTracker()
        body = b'{"usage":{"prompt_cache_hit_tokens":300,"prompt_cache_miss_tokens":7}}'
        record_cache_from_body(tracker, "m", body, session="s2")
        session = tracker.snapshot()["sessions"][0]
        self.assertEqual(session["session"], "s2")
        self.assertEqual(session["cache_hit_tokens"], 300)
        self.assertEqual(session["cache_miss_tokens"], 7)

    def test_record_cache_from_body_ignores_a_non_json_body(self) -> None:
        from opencode_go_proxy.upstream import record_cache_from_body

        tracker = CacheTracker()
        record_cache_from_body(tracker, "m", b"<html>502</html>", session="s1")
        self.assertEqual(tracker.snapshot()["sessions"], [])


class IncludeUsageTests(unittest.TestCase):
    def test_streaming_chat_body_gains_include_usage(self) -> None:
        from opencode_go_proxy.upstream import with_include_usage

        body = with_include_usage({"stream": True, "messages": []}, stream=True)
        self.assertEqual(body["stream_options"], {"include_usage": True})
        self.assertEqual(body["messages"], [])

    def test_client_stream_options_keep_their_other_keys(self) -> None:
        from opencode_go_proxy.upstream import with_include_usage

        body = with_include_usage(
            {"stream_options": {"foo": 1}}, stream=True
        )
        self.assertEqual(body["stream_options"], {"foo": 1, "include_usage": True})

    def test_an_explicit_include_usage_decision_is_respected(self) -> None:
        from opencode_go_proxy.upstream import with_include_usage

        body = {"stream_options": {"include_usage": False}}
        self.assertIs(with_include_usage(body, stream=True), body)

    def test_non_stream_bodies_are_untouched(self) -> None:
        from opencode_go_proxy.upstream import with_include_usage

        body = {"messages": []}
        self.assertIs(with_include_usage(body, stream=False), body)

    def test_the_input_body_is_not_mutated(self) -> None:
        from opencode_go_proxy.upstream import with_include_usage

        body = {"stream": True}
        with_include_usage(body, stream=True)
        self.assertNotIn("stream_options", body)


class UsageTapTests(unittest.TestCase):
    def test_usage_chunk_is_captured_and_the_line_is_unchanged(self) -> None:
        from opencode_go_proxy.upstream import UsageTap

        tap = UsageTap()
        line = b'data: {"choices":[],"usage":{"prompt_cache_hit_tokens":5,"prompt_cache_miss_tokens":1}}\n'
        self.assertIs(tap(line), line)
        self.assertEqual(tap.usage, {"prompt_cache_hit_tokens": 5, "prompt_cache_miss_tokens": 1})

    def test_content_chunks_without_usage_pass_through(self) -> None:
        from opencode_go_proxy.upstream import UsageTap

        tap = UsageTap()
        line = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n'
        self.assertIs(tap(line), line)
        self.assertIsNone(tap.usage)

    def test_a_null_usage_is_not_captured(self) -> None:
        from opencode_go_proxy.upstream import UsageTap

        tap = UsageTap()
        tap(b'data: {"choices":[{"delta":{}}],"usage":null}\n')
        self.assertIsNone(tap.usage)

    def test_non_json_and_done_lines_are_safe(self) -> None:
        from opencode_go_proxy.upstream import UsageTap

        tap = UsageTap()
        for line in (b"data: [DONE]\n", b": keepalive\n\n", b'data: {"usage":\n'):
            self.assertIs(tap(line), line)
        self.assertIsNone(tap.usage)


if __name__ == "__main__":
    unittest.main()
