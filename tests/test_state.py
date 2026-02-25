"""
Unit tests for the SQLite state store (clients/state.py).

Uses pytest's tmp_path for fully isolated, per-test SQLite files.
(In-memory :memory: databases can't be used here because StateStore opens a
new connection on every call — each connection to ':memory:' is a separate
empty database, so tables created in __init__ would be invisible to later calls.)
"""
from __future__ import annotations

import pytest
import threading
from pathlib import Path

from clients.state import StateStore


@pytest.fixture
def store(tmp_path):
    """Fresh file-backed StateStore in a temp directory for each test."""
    return StateStore(db_path=tmp_path / "test_state.db")


class TestProcessedIds:
    def test_has_processed_false_initially(self, store):
        assert not store.has_processed("test_ns", "id-1")

    def test_mark_and_check(self, store):
        store.mark_processed("test_ns", "id-1")
        assert store.has_processed("test_ns", "id-1")

    def test_different_namespaces_isolated(self, store):
        store.mark_processed("ns-a", "id-1")
        assert not store.has_processed("ns-b", "id-1")

    def test_mark_many(self, store):
        ids = [f"id-{i}" for i in range(10)]
        store.mark_many_processed("ns", ids)
        for id_ in ids:
            assert store.has_processed("ns", id_)

    def test_mark_idempotent(self, store):
        """Marking the same ID twice should not raise."""
        store.mark_processed("ns", "id-dup")
        store.mark_processed("ns", "id-dup")
        assert store.has_processed("ns", "id-dup")

    def test_purge_old_removes_stale(self, store):
        """Inject an old record directly and verify purge removes it."""
        import sqlite3
        conn = sqlite3.connect(str(store._db_path))
        conn.execute(
            "INSERT INTO processed_ids (namespace, id, created_at) VALUES (?, ?, ?)",
            ("ns", "old-id", "2020-01-01 00:00:00"),
        )
        conn.commit()
        conn.close()

        removed = store.purge_old("ns", keep_days=30)
        assert removed == 1
        assert not store.has_processed("ns", "old-id")

    def test_purge_old_keeps_recent(self, store):
        store.mark_processed("ns", "recent-id")
        removed = store.purge_old("ns", keep_days=30)
        assert removed == 0
        assert store.has_processed("ns", "recent-id")


class TestSnapshots:
    def test_get_snapshot_empty(self, store):
        assert store.get_snapshot("b2_deals") == {}

    def test_set_and_get_snapshot(self, store):
        data = {"deal-1": {"name": "Nike", "stage": "Negotiation"}}
        store.set_snapshot("b2_deals", data)
        result = store.get_snapshot("b2_deals")
        assert result == data

    def test_update_snapshot(self, store):
        store.set_snapshot("ns", {"key": "v1"})
        store.set_snapshot("ns", {"key": "v2"})
        assert store.get_snapshot("ns") == {"key": "v2"}

    def test_different_namespaces_isolated(self, store):
        store.set_snapshot("ns-a", {"a": 1})
        store.set_snapshot("ns-b", {"b": 2})
        assert store.get_snapshot("ns-a") == {"a": 1}
        assert store.get_snapshot("ns-b") == {"b": 2}

    def test_snapshot_survives_large_data(self, store):
        """Snapshots with 500 deals should round-trip cleanly."""
        large = {f"deal-{i}": {"name": f"Deal {i}", "prob": i % 100} for i in range(500)}
        store.set_snapshot("large_ns", large)
        result = store.get_snapshot("large_ns")
        assert len(result) == 500
        assert result["deal-250"]["prob"] == 50


class TestConcurrency:
    def test_concurrent_mark_processed(self, store):
        """Multiple threads writing to processed_ids should not corrupt the DB."""
        errors = []

        def write(thread_id: int):
            try:
                for i in range(20):
                    store.mark_processed("concurrent_ns", f"thread-{thread_id}-id-{i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=write, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        # All 100 IDs should be marked
        for t in range(5):
            for i in range(20):
                assert store.has_processed("concurrent_ns", f"thread-{t}-id-{i}")
