"""Normalization and hashing: the validation boundary and change-detection primitive."""

from __future__ import annotations

import pytest

from integration_sync.errors import PoisonError
from integration_sync.hashing import content_hash
from integration_sync.normalize import normalize

from .conftest import make_raw


def test_valid_record_normalizes():
    record = normalize(make_raw("k1@sketch", occurred_at="2026-03-02T09:00:00+00:00"))
    assert record.natural_key == "k1@sketch"
    assert record.content_hash  # non-empty digest
    assert record.occurred_at.isoformat() == "2026-03-02T09:00:00+00:00"


def test_missing_key_is_poison():
    with pytest.raises(PoisonError) as exc:
        normalize(make_raw(None, occurred_at="2026-03-02T09:00:00+00:00"))
    assert exc.value.natural_key is None


def test_bad_date_is_poison():
    with pytest.raises(PoisonError) as exc:
        normalize(make_raw("k2@sketch", occurred_at="nonsense"))
    assert exc.value.natural_key == "k2@sketch"


def test_invalid_email_is_poison():
    with pytest.raises(PoisonError):
        normalize(make_raw("k3@sketch", occurred_at="2026-03-02T09:00:00+00:00",
                           email="no-at-sign"))


def test_naive_timestamp_is_treated_as_utc():
    record = normalize(make_raw("k4@sketch", occurred_at="2026-03-02T09:00:00"))
    assert record.occurred_at.isoformat() == "2026-03-02T09:00:00+00:00"


def test_hash_is_order_independent():
    a = content_hash({"x": "1", "y": "2"})
    b = content_hash({"y": "2", "x": "1"})
    assert a == b


def test_hash_changes_on_content_change():
    a = content_hash({"subject": "hello"})
    b = content_hash({"subject": "hello!"})
    assert a != b


def test_hash_ignores_surrounding_whitespace():
    assert content_hash({"s": "hello"}) == content_hash({"s": "  hello  "})


def test_same_content_same_hash_across_records():
    r1 = normalize(make_raw("dup@sketch", occurred_at="2026-03-02T09:00:00+00:00"))
    r2 = normalize(make_raw("dup@sketch", occurred_at="2026-03-02T09:00:00+00:00"))
    assert r1.content_hash == r2.content_hash
