"""A local/test run with the write flag OFF must not write to the store AT ALL.

Found the hard way during round 5: D1_WRITE_ENABLED=0 did not cover the api_usage metering
flush (d1_store._upsert has no gate of its own), so a locally served candidate with a token in
its environment wrote 8 api_usage rows to PRODUCTION D1.
"""
from __future__ import annotations

import os

import pytest

import d1_harness as H
import d1_store

os.environ.setdefault("D1_WRITE_ENABLED", "1")

ROW = {"bucket": "day", "period": "2026-10-01", "source": "cfbd", "calls": 1,
       "provider_remaining": None, "provider_limit": 30000, "provider_used": 1,
       "updated_at": "2026-10-01T00:00:00+00:00"}


@pytest.fixture
def conn(monkeypatch):
    c = H.open_sqlite("append")
    return c


def test_write_flag_off_blocks_the_metering_flush(monkeypatch, conn):
    monkeypatch.setenv("D1_WRITE_ENABLED", "0")
    monkeypatch.setenv("CF_D1_TOKEN", "present-but-must-not-be-used")
    st = H.patch_d1(monkeypatch, conn)

    n = d1_store.upsert_api_usage([dict(ROW)])

    assert n == 0, "a write-flagged-off process must write nothing"
    inserts = [s for s in st.calls if "INSERT INTO api_usage" in s]
    assert not inserts, f"the metering flush reached the store anyway: {inserts}"


def test_write_flag_on_still_flushes_the_ledger(monkeypatch, conn):
    """The guard must not disable the ledger of record where writes ARE enabled."""
    monkeypatch.setenv("D1_WRITE_ENABLED", "1")
    monkeypatch.setenv("CF_D1_TOKEN", "ok")
    H.patch_d1(monkeypatch, conn)

    n = d1_store.upsert_api_usage([dict(ROW)])

    assert n >= 1, "with writes enabled the metering ledger must still persist"