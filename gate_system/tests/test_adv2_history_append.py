"""ADV2 — HistoryStore.append() crashes on a valid-JSON-but-not-a-list file.

Round 1 hardened HistoryStore.recent() to tolerate a malformed history file
(`if not isinstance(records, list): return []`). But the WRITE path was not
hardened symmetrically. database.read_json returns its default only for a
missing/undecodable file; a file that is valid JSON of the WRONG shape (e.g. a
JSON object left by a legacy format, a hand edit, or a partially-migrated store)
is returned as-is. append() then does `records.append(...)` on a dict ->
AttributeError.

This propagates out of AccessDecision.handle() (the history.append call there is
NOT wrapped), so once history.json is a JSON object every access event raises:
for a GRANTED car the relay already fired but the owner notification, user-event
journal entry and history record are all skipped, and the error repeats for
every subsequent car — while /history silently shows nothing (recent() swallows
it). Logging/alerting is effectively dead with no visible cause.
"""
import json
import os
import tempfile

import pytest

from history import HistoryStore
from models import Event, CheckResult, PlateNumber


def _store_with(content):
    d = tempfile.mkdtemp()
    path = os.path.join(d, "history.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return HistoryStore(path)


def test_append_survives_history_file_that_is_a_json_object():
    # A valid JSON object (not a list) — e.g. legacy/hand-edited/corrupt store.
    store = _store_with(json.dumps({"events": []}))
    evt = Event.now(PlateNumber("А123ВС777"), CheckResult.GRANTED, gate_opened=True)

    # recent() tolerates it (returns []). append() must not crash either.
    assert store.recent() == []
    store.append(evt)  # currently raises AttributeError: 'dict' object has no attribute 'append'

    # After a successful append the event should be retrievable.
    got = store.recent()
    assert len(got) == 1 and got[0].plate.value == "А123ВС777"


def test_access_decision_does_not_break_on_corrupt_history_file():
    """The corrupt-history crash must not escape AccessDecision.handle()."""
    from pipeline import AntiReplayGuard, AccessDecision

    class _WL:
        def is_allowed(self, p):
            return True

    class _Gate:
        def open(self, source):
            pass

    class _Notif:
        def notify_known(self, e):
            pass
        def notify_unknown(self, e):
            pass

    store = _store_with(json.dumps({"events": []}))
    dec = AccessDecision(_WL(), _Gate(), store, _Notif(), AntiReplayGuard(window_s=1))

    # Should return a result, not raise. Currently raises AttributeError.
    res = dec.handle(PlateNumber("А123ВС777"), frame=None)
    assert res.gate_opened is True
