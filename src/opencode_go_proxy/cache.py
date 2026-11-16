from __future__ import annotations

import threading
from typing import Any

Json = dict[str, Any]

# Sessions kept in the snapshot. A long-lived proxy sees a new session id per
# conversation (and per CLI process that derives one), so an unbounded map
# would grow without limit; the least recently active session is evicted.
DEFAULT_MAX_SESSIONS = 200


class CacheTracker:
    """Rolling prefix-cache accounting for upstream chat-completions traffic.

    Every proxied request reports how many prompt tokens were served from the
    provider's prefix cache (hit) versus re-processed (miss). The tracker sums
    those per model and per session and exposes the aggregate hit ratio, which
    is the metric that says whether the proxy is producing cache-stable
    requests.

    The per-session view answers a second question: providers keep a prompt
    prefix cached for hours, so a conversation that keeps reusing the same
    prefix should report a hit on every request after the first. A request
    that reports zero hits *after* the session already had a hit means the
    prefix was broken mid-conversation (a changed system prompt, a reordered
    tool list, or a different entry point), which is what ``prefix_lost``
    counts. ``cold_starts`` counts the first request of a session, where a
    full miss is expected and harmless.
    """

    def __init__(self, max_sessions: int = DEFAULT_MAX_SESSIONS) -> None:
        self._lock = threading.Lock()
        self._hit: dict[str, int] = {}
        self._miss: dict[str, int] = {}
        self._requests: dict[str, int] = {}
        self._sessions: dict[str, Json] = {}
        self._max_sessions = max_sessions
        self._seq = 0
        # Lifetime counters, kept apart from the per-session map so an evicted
        # session's history still shows in the totals instead of making them
        # shrink as sessions age out.
        self._total_sessions = 0
        self._cold_starts = 0
        self._prefix_lost = 0

    def record(
        self,
        model: str | None,
        hit: int,
        miss: int,
        session: str | None = None,
    ) -> None:
        if hit < 0 or miss < 0:
            return
        key = model or "unknown"
        with self._lock:
            self._hit[key] = self._hit.get(key, 0) + hit
            self._miss[key] = self._miss.get(key, 0) + miss
            self._requests[key] = self._requests.get(key, 0) + 1
            if session:
                self._record_session(session, key, hit, miss)

    def _record_session(self, session: str, model: str, hit: int, miss: int) -> None:
        entry = self._sessions.get(session)
        new = entry is None
        if entry is None:
            entry = {
                "session": session,
                "model": model,
                "requests": 0,
                "hit": 0,
                "miss": 0,
                "cold_starts": 0,
                "prefix_lost": 0,
                "seq": 0,
            }
            self._sessions[session] = entry
            self._total_sessions += 1
        prior_hit = entry["hit"]
        entry["model"] = model
        entry["requests"] += 1
        entry["hit"] += hit
        entry["miss"] += miss
        self._seq += 1
        entry["seq"] = self._seq
        if new:
            # Evict after the new session has its own sequence number: evicting
            # before would compare against the placeholder and drop the entry
            # that was just created.
            while len(self._sessions) > self._max_sessions:
                self._evict_oldest()
        if hit == 0 and miss > 0:
            # A zero-hit request only signals a broken prefix once the session
            # has already been served from cache; before that it is a cold
            # start and a full miss is expected.
            if prior_hit > 0:
                entry["prefix_lost"] += 1
                self._prefix_lost += 1
            else:
                entry["cold_starts"] += 1
                self._cold_starts += 1

    def _evict_oldest(self) -> None:
        oldest = min(self._sessions, key=lambda name: self._sessions[name]["seq"])
        del self._sessions[oldest]

    def snapshot(self) -> Json:
        with self._lock:
            models = sorted(set(self._hit) | set(self._miss))
            per_model = []
            total_hit = 0
            total_miss = 0
            total_requests = 0
            for model in models:
                hit = self._hit.get(model, 0)
                miss = self._miss.get(model, 0)
                total_hit += hit
                total_miss += miss
                total_requests += self._requests.get(model, 0)
                per_model.append(self._row(model, hit, miss, self._requests.get(model, 0)))
            sessions = sorted(
                self._sessions.values(), key=lambda entry: entry["seq"], reverse=True
            )
            totals = self._row("all", total_hit, total_miss, total_requests)
            totals["sessions"] = self._total_sessions
            totals["cold_starts"] = self._cold_starts
            totals["prefix_lost"] = self._prefix_lost
            return {
                "models": per_model,
                "sessions": [self._session_row(entry) for entry in sessions],
                "totals": totals,
            }

    @staticmethod
    def _row(model: str, hit: int, miss: int, requests: int) -> Json:
        total = hit + miss
        return {
            "model": model,
            "cache_hit_tokens": hit,
            "cache_miss_tokens": miss,
            "requests": requests,
            "hit_ratio": round(hit / total, 6) if total else None,
        }

    @staticmethod
    def _session_row(entry: Json) -> Json:
        row = CacheTracker._row(
            str(entry["model"]),
            int(entry["hit"]),
            int(entry["miss"]),
            int(entry["requests"]),
        )
        return {
            "session": entry["session"],
            **row,
            "cold_starts": entry["cold_starts"],
            "prefix_lost": entry["prefix_lost"],
        }
