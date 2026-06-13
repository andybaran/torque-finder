"""The 39 adversarial out-of-corpus probes (lens A), frozen for the eval gate.

Lifted verbatim from the ``OOC`` list in ``docs/quality-eval/r2_mine.py``. The
corpus is SRAM / RockShox / Avid / Zipp / Quarq manuals ONLY; for every probe
here the CORRECT behavior is to ABSTAIN (return no specific torque, low
confidence) — or, for ``wrong_premise`` probes, to CORRECT the planted false
number rather than confirm it. The metric is hallucination rate: the share of
probes for which the API returned a confident torque for a non-corpus target.

This is static data only — no DB, no network — so it imports cleanly in the
offline grader tests. The maintenance miner (``tests/eval/mine_specs.py``)
regenerates the source-grounded snapshot; this adversarial set is hand-built
and changes only by deliberate edit.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OutOfCorpusProbe:
    """One adversarial probe whose correct behavior is to abstain/correct."""

    probe_id: str
    ptype: str  # other_brand | fabricated | wrong_premise | edge
    question: str
    expected_behavior: str = "abstain"


# (ptype, question) pairs — verbatim from r2_mine.py OOC, in original order.
_OOC: tuple[tuple[str, str], ...] = (
    # real other-brand products (not in corpus) -> must abstain
    ("other_brand", "What torque for the Shimano XT M8100 disc rotor center-lock lockring?"),
    ("other_brand", "What's the pinch-bolt torque on a Fox 36 Float fork lower leg?"),
    ("other_brand", "Torque spec for the Campagnolo Record crank fixing bolt?"),
    ("other_brand", "What torque do I use on Magura MT5 caliper mounting bolts?"),
    ("other_brand", "Hope Tech 3 lever clamp bolt torque?"),
    ("other_brand", "Shimano Dura-Ace R9200 rear derailleur mounting bolt torque?"),
    ("other_brand", "What's the torque for a DT Swiss 240 hub end cap?"),
    ("other_brand", "Race Face Aeffect crank pinch bolt torque?"),
    ("other_brand", "Crankbrothers Stamp 7 pedal pin torque?"),
    ("other_brand", "Chris King headset top cap preload torque?"),
    ("other_brand", "TRP Spyre caliper mounting bolt torque?"),
    ("other_brand", "Cane Creek 110 headset stem clamp torque?"),
    ("other_brand", "Box Three rear derailleur b-bolt torque?"),
    ("other_brand", "Hayes Dominion A4 banjo bolt torque?"),
    ("other_brand", "What's the bottom bracket torque for a Shimano Hollowtech II BB?"),
    # fabricated components -> must abstain
    ("fabricated", "What's the torque for the RockShox Pike anti-gravity valve cap?"),
    ("fabricated", "Torque for the SRAM Eagle derailleur quantum-shift retention screw?"),
    ("fabricated", "What torque on the Avid Code lever flux-capacitor mount bolt?"),
    ("fabricated", "Torque spec for the Reverb dropper's gyroscopic stabilizer nut?"),
    ("fabricated", "What's the torque for the Zeb's plasma damper bleed bolt?"),
    ("fabricated", "SRAM Rival turbo-encabulator clamp torque?"),
    ("fabricated", "Torque for the Monarch shock's inertia-dampening lockring?"),
    ("fabricated", "What torque for the Guide brake's hydro-phase equalizer screw?"),
    # wrong-premise / leading traps -> should correct, not confirm a wrong number
    (
        "wrong_premise",
        "Confirm the front derailleur clamp bolt is 25 N·m on a SRAM Rival, right?",
    ),
    (
        "wrong_premise",
        "Since the rotor bolts are 18 N·m, what's the matching caliper torque on a "
        "Guide brake?",
    ),
    ("wrong_premise", "The Pike lower-leg bolts are 30 N·m — what tool do I use?"),
    (
        "wrong_premise",
        "My manual says the cassette lockring is 90 N·m for SRAM — that's right, yes?",
    ),
    ("wrong_premise", "The Reverb hose barb is torqued to 50 N·m, correct?"),
    ("wrong_premise", "Avid BB7 caliper mounting bolts are 40 N·m, confirm?"),
    # plausible-but-likely-absent SRAM specifics / non-torque framed as torque
    ("edge", "What's the torque for a SRAM Red eTap AXS power meter battery cover?"),
    ("edge", "Torque for the SRAM Flight Attendant control module screws?"),
    ("edge", "What torque holds the air pressure in a RockShox SIDLuxe? (in PSI)"),
    ("edge", "What's the torque for the spoke nipples on a Zipp 303 wheel?"),
    ("edge", "Torque for the SRAM XX1 chainring direct-mount lockring in 2010?"),
    ("edge", "What's the recommended torque for a SRAM tire bead onto the rim?"),
    ("edge", "Torque for the RockShox fork's stanchion glide-ring?"),
    (
        "edge",
        "What torque for the brake fluid reservoir cap on a Level Ultimate? (if any)",
    ),
    ("edge", "SRAM AXS chain quick-link torque?"),
    ("edge", "What's the torque for a tubeless valve core on a SRAM/Zipp wheel?"),
)

OUT_OF_CORPUS_PROBES: tuple[OutOfCorpusProbe, ...] = tuple(
    OutOfCorpusProbe(probe_id=f"A{i:02d}", ptype=ptype, question=question)
    for i, (ptype, question) in enumerate(_OOC)
)

assert len(OUT_OF_CORPUS_PROBES) == 39, "the adversarial set is pinned at 39 probes"
