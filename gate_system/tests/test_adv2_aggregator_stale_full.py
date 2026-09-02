"""ADV2 — Aggregator round 2: stale 'fully-read' plate causes a FALSE OPEN.

Round 1 fixed the cross-car phantom by 'preferring a fully-read plate' for the
winning main. But the pool of full reads (`_fulls`, a deque(maxlen=25)) is only
evicted by COUNT, never by time, and `best_full` is preferred over the fresh
region UNCONDITIONALLY. So a full read of a whitelisted plate that happened long
ago keeps authorizing any later car whose main (letter+3digits+2letters) matches
— even though that later car's region (and thus its real, non-whitelisted plate)
is different.

Two legitimately different Russian plates can share the same main and differ only
in the region code (same series, different region), e.g.:
    Х002МС777  (whitelisted, allowed)
    Х002МС133  (a DIFFERENT real car, NOT allowed)

Production builds PlateAggregator(confirm=cfg.access.confirm_frames) with
reemit_after_s defaulting to 200s (main.py) — exactly the parameters used here.
"""
from aggregator import PlateAggregator


def test_stale_full_read_authorizes_a_different_car_sharing_the_main():
    clock = [0.0]
    agg = PlateAggregator(confirm=2, window=25, reemit_after_s=200.0,
                          clock=lambda: clock[0])

    # 1) A whitelisted car passes and is read CLEANLY twice (full reads).
    agg.add("Х002МС777")
    first = agg.add("Х002МС777")
    assert first is not None and first.value == "Х002МС777"

    # 2) The whitelisted car leaves; time passes beyond the re-emit window.
    #    No further clean full reads occur, so the stale Х002МС777 full reads are
    #    never evicted from the 25-slot window.
    clock[0] = 1000.0

    # 3) A DIFFERENT car (real plate Х002МС133, NOT whitelisted) is only read
    #    partially — main and region on separate frames, never a clean full read.
    emitted = []
    for _ in range(8):
        got = agg.add("Х002МС")
        if got is not None:
            emitted.append(got.value)
        got = agg.add("133")
        if got is not None:
            emitted.append(got.value)

    assert "Х002МС777" not in emitted, (
        "FALSE OPEN: a stale full read of whitelisted Х002МС777 authorized a "
        f"different car (real region 133). Emitted: {emitted}"
    )
