"""ADV2 — AntiReplayGuard: a DENIED read poisons the guard and suppresses the
car's OWN later grant.

AccessDecision.handle() calls guard.mark_opened(plate) for EVERY processed read,
including DENIED (unknown) ones. If the owner then adds that plate to the
whitelist within the anti-replay window (config antireplay_window_s = 180s in
config/config.json), the next read is GRANTED but is suppressed by anti-replay —
so the gate does NOT open. A car that was denied and then legitimately authorized
is locked out for up to the whole window (up to 3 minutes with shipped config),
with no way for the owner to force it except waiting.
"""
import pipeline
from pipeline import AntiReplayGuard, AccessDecision
from models import PlateNumber


class _WL:
    def __init__(self):
        self.s = set()
    def is_allowed(self, p):
        return p.value in self.s


class _Gate:
    def __init__(self):
        self.opens = 0
    def open(self, source):
        self.opens += 1


class _Hist:
    def append(self, e):
        pass


class _Notif:
    def notify_known(self, e):
        pass
    def notify_unknown(self, e):
        pass


def test_denied_read_poisons_guard_and_blocks_later_grant(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(pipeline.time, "monotonic", lambda: clock[0])

    wl = _WL()
    gate = _Gate()
    guard = AntiReplayGuard(window_s=180)  # shipped config value
    dec = AccessDecision(wl, gate, _Hist(), _Notif(), guard)
    plate = PlateNumber("А123ВС777")

    # 1) Unknown car -> DENIED. This marks the guard for this plate.
    r1 = dec.handle(plate, frame=None)
    assert r1.gate_opened is False

    # 2) Owner adds the plate to the whitelist, 5s later (well within window).
    wl.s.add("А123ВС777")
    clock[0] = 105.0

    # 3) Same car reads again — now legitimately GRANTED. The gate MUST open.
    r2 = dec.handle(plate, frame=None)

    assert r2.gate_opened is True and gate.opens == 1, (
        "Legitimate grant suppressed: a prior DENIED read poisoned the "
        "anti-replay guard, so newly-whitelisted car stays locked out "
        f"(suppressed_by_antireplay={r2.suppressed_by_antireplay})"
    )
