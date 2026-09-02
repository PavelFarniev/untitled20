"""Adversarial SECURITY tests for gate_system.

Each test asserts the SAFE/correct behaviour and therefore FAILS against the
current code, demonstrating a real security defect. Do NOT "fix" these by
weakening the assertion — the point is to prove the defect exists.
"""
from aggregator import PlateAggregator
from utils import normalize_plate
from models import PlateNumber
import pipeline


class TestAggregatorPhantomPlate:
    """CRITICAL: PlateAggregator votes the main part and the region part of a
    plate SEPARATELY. Fragments from two DIFFERENT cars can therefore be
    combined into a plate string that was NEVER actually observed as a whole.
    If that fabricated string happens to be whitelisted, the gate opens for a
    car that was never present -> false gate opening / access bypass.
    """

    def test_aggregator_never_emits_a_plate_never_seen_whole(self):
        agg = PlateAggregator(confirm=2)
        # Car A repeatedly reads main "Х002МС" with region 777/888.
        # Car B repeatedly reads region 197 with completely different mains.
        reads = ["X002MC777", "X002MC777", "X002MC888",
                 "A123BC197", "K555OP197", "E111KX197"]

        seen_full = set()
        emitted = []
        for r in reads:
            full = normalize_plate(r)
            if full is not None:
                seen_full.add(full.value)
            got = agg.add(r)
            if got is not None:
                emitted.append(got.value)

        phantom = [p for p in emitted if p not in seen_full]
        assert phantom == [], (
            "Aggregator fabricated plate(s) that were NEVER seen as a whole "
            f"in any single frame: {phantom}. Seen-whole reads were: {sorted(seen_full)}"
        )

    def test_phantom_plate_opens_gate_for_car_never_present(self):
        """End-to-end proof: whitelist contains Х002МС197. That exact plate was
        never fully read, yet the aggregator emits it and the gate opens."""
        whitelisted = "Х002МС197"

        class WL:
            def is_allowed(self, plate):
                return plate.value == PlateNumber(whitelisted).value

        class Gate:
            def __init__(self):
                self.opened = 0

            def open(self, source):
                self.opened += 1

        class Hist:
            def append(self, e):
                pass

        class Notif:
            def notify_known(self, e):
                pass

            def notify_unknown(self, e):
                pass

        gate = Gate()
        decision = pipeline.AccessDecision(
            WL(), gate, Hist(), Notif(), pipeline.AntiReplayGuard(window_s=30)
        )
        agg = PlateAggregator(confirm=2)

        # None of these frames is the whitelisted plate Х002МС197.
        reads = ["X002MC777", "X002MC777", "X002MC888",
                 "A123BC197", "K555OP197", "E111KX197"]
        for r in reads:
            plate = agg.add(r)
            if plate is not None:
                decision.handle(plate, frame=None)

        assert gate.opened == 0, (
            "Gate opened for a whitelisted plate that was assembled from two "
            "different cars' fragments and never actually appeared in front of "
            "the camera (phantom-plate access bypass)."
        )
