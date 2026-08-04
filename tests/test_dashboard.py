"""The ops dashboard: does it actually render, and does it degrade politely when empty.

Streamlit's own ``AppTest`` harness runs the app in a real script-run context, so an
exception in a tile surfaces here rather than as a red box somebody notices in a demo. That
is the point: a smoke test that only imported the module would pass while every tile threw.

The compile check runs everywhere. The render checks need streamlit and skip without it, so
a contributor who only wants the sync engine is not forced to install a UI framework.
"""

from __future__ import annotations

import py_compile
from pathlib import Path

import pytest

from integration_sync.crm_store import CrmStore
from integration_sync.hubspot_source import (
    HubSpotClient,
    RecordedTransport,
    contact_email_resolver,
    deal_projector,
    read_hubspot_contacts,
    read_hubspot_deals,
)
from integration_sync.models import SOURCE_HUBSPOT_CONTACTS, SOURCE_HUBSPOT_DEALS
from integration_sync.sync_engine import SyncEngine

REPO = Path(__file__).resolve().parent.parent
APP = REPO / "dashboard" / "app.py"
FIXTURES = REPO / "fixtures" / "hubspot"


def build_populated_db(path: Path) -> None:
    """A real store, built by the real engine, with a real dead-letter row in it."""
    store = CrmStore(path)
    store.init_schema()
    engine = SyncEngine(
        store, sleep=lambda _d: None, projectors={SOURCE_HUBSPOT_DEALS: deal_projector}
    )

    def client(cassette: str) -> HubSpotClient:
        return HubSpotClient(
            RecordedTransport.from_dir(FIXTURES, cassette), sleep=lambda _d: None, page_size=2
        )

    engine.sync_source(
        SOURCE_HUBSPOT_CONTACTS, lambda since: read_hubspot_contacts(client("contacts"), since)
    )
    resolver = contact_email_resolver(store)
    engine.sync_source(
        SOURCE_HUBSPOT_DEALS,
        lambda since: read_hubspot_deals(client("deals"), since, resolve_contact_email=resolver),
    )
    store.close()


def test_the_dashboard_module_compiles():
    py_compile.compile(str(APP), doraise=True)


def test_the_dashboard_renders_every_panel_without_raising(tmp_path, monkeypatch):
    app_test = pytest.importorskip("streamlit.testing.v1")
    db = tmp_path / "dash.db"
    build_populated_db(db)
    monkeypatch.setenv("SYNC_DB", str(db))

    run = app_test.AppTest.from_file(str(APP), default_timeout=60).run()

    assert [str(e.value) for e in run.exception] == []
    labels = {metric.label for metric in run.metric}
    assert {"Records in the store", "Dead-letter depth", "ingested", "upserted"} <= labels
    assert {s.value for s in run.subheader} == {
        "Cursors",
        "Writes over time",
        "Per-source throughput",
        "Funnel",
        "Dead-letter queue",
        "Deals by stage",
    }


def test_the_rendered_numbers_are_the_ones_the_metrics_module_computed(tmp_path, monkeypatch):
    # The tiles must not recompute anything of their own. If this drifts, a number on the
    # page is coming from somewhere that has no test behind it.
    app_test = pytest.importorskip("streamlit.testing.v1")
    from integration_sync import ops_metrics as om

    db = tmp_path / "dash.db"
    build_populated_db(db)
    monkeypatch.setenv("SYNC_DB", str(db))

    store = CrmStore(db)
    expected = om.funnel(store)
    store.close()

    run = app_test.AppTest.from_file(str(APP), default_timeout=60).run()
    shown = {metric.label: metric.value for metric in run.metric}
    assert shown["ingested"] == str(expected.ingested)
    assert shown["upserted"] == str(expected.upserted)
    assert shown["Dead-letter depth"] == str(expected.dead_lettered_open)


def test_a_missing_store_is_explained_rather_than_crashed_on(tmp_path, monkeypatch):
    app_test = pytest.importorskip("streamlit.testing.v1")
    monkeypatch.setenv("SYNC_DB", str(tmp_path / "does-not-exist.db"))

    run = app_test.AppTest.from_file(str(APP), default_timeout=60).run()

    assert [str(e.value) for e in run.exception] == []
    assert any("No sync store" in error.value for error in run.error)
    # It tells the reader the exact command that fixes it.
    assert any("hubspot_demo.py" in md.value for md in run.markdown)
