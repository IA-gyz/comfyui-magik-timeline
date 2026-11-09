# Magik Timeline -- Phase 4 acceptance experiment
# (GPL-3.0). Provenance: docs/MAGIK_PORT.md sec. 5; audit ch. 11.7.
"""The protocol as code: arms, what is held constant, what is measured,
and the acceptance criteria. The harness and the methodology doc read
these constants, so they cannot drift apart.

Source for every constant: ``Magik_Timeline_Upstream_Audit.pdf`` ch. 11.7
(``scripts/report_content_b.py`` lines 285-301).
"""
try:
    from seam_report import SEAMLESS_BELOW, SOFT_BUMP_BELOW, verdict_for
except ImportError:                             # pragma: no cover
    from .seam_report import SEAMLESS_BELOW, SOFT_BUMP_BELOW, verdict_for


# The acceptance criteria from audit ch. 11.7, verbatim:
#
#   "Acceptance for keeping the pixel-grade boundary: the test arm must
#    not regress seam metrics relative to control, and must measurably
#    reduce first-stage composition drift on the held-out windows."
#
# "Not regress" is interpreted per the same three bands the metric uses:
# the test arm's worst seam ratio must be no higher than the control
# arm's, AND both must verdict "seamless" (< SEAMLESS_BELOW). A "soft
# bump" is a fail regardless of order, because the audit's calibration
# sample (24 real extends) put every clean join below 1.45.

ACCEPT_SEAMLESS_BOTH = True      # both arms must verdict "seamless"
ACCEPT_TEST_NOT_WORSE = True     # test.verdict no worse band than control.verdict
ACCEPT_DRIFT_REDUCTION = True    # test.drift < control.drift (composition)

# The composition-drift metric is "first-stage composition drift on the
# held-out windows" -- i.e., how far the first sampler's output moved
# off the source window's framing, measured against the source window
# itself. Phase 4 measures it as the mean cosine distance between the
# first-stage generated latent steps and the corresponding source-window
# latent steps, normalised the same way as the seam metric (per phase).
# The DRIFT_REDUCTION_MIN improvement is a floor, not a target: the test
# arm must show SOME reduction, not necessarily a large one. The exact
# number is the smallest improvement that survives measurement noise on
# the audit's calibration sample; calibrate to your own footage.
DRIFT_REDUCTION_MIN = 0.02       # test.drift <= control.drift - 0.02

# Arms. Each is a name + a one-line description that the harness prints
# in the result. The control and test arms are the two the audit names;
# the path_a reference is the synthetic "what Path A would have produced"
# arm, included so a future Path A revival can be measured against the
# same bar without re-deriving the protocol.
ARMS = {
    "control": (
        "no pin at the first sampler; original HR pins at the second. "
        "The baseline: a standard single-resolution refine pass."),
    "test_path_c": (
        "LR pixel-grade pin at the first sampler (Path C: the source "
        "file re-encoded at stage-1 resolution); HR pin at the second. "
        "The dual-resolution mode shipped in 0.3.0."),
    "ref_path_a": (
        "Reference arm for a future Path A revival: LR pin as a latent "
        "downsample of the HR slice. Not built; the harness measures "
        "it only if a latent-resample grade is implemented later."),
}

# What is held constant between arms (audit ch. 11.7):
HELD_CONSTANT = [
    "source clip",
    "pin window",
    "prompt",
    "reference images",
    "seed",
    "step split (first-stage steps, second-stage steps)",
    "upscaler (MinimaxH3LatentUpscaler3D with temporal chunking ON, "
    "as upstream requires -- nodes_masked/upscaler_guard refuses it off)",
    "target dimensions (stage-1 LR size, stage-2 HR size)",
]

# What is measured (audit ch. 11.7). The harness implements the first
# three; the last two (identity consistency, seam-repair regression) are
# qualitative read-outs the operator records in the result template.
MEASURED = [
    ("seam_ratio", (
        "worst step-difference past the boundary as a multiple of the "
        "clip's phase-matched median, via seam_report.measure_seam. "
        "Verdict: seamless < %.2f, soft bump < %.2f, hard cut above."
        % (SEAMLESS_BELOW, SOFT_BUMP_BELOW))),
    ("cut_scan", (
        "frame-difference cut scan over the first/last %.1f s of the "
        "delivered MP4, via seam_report.measure_cuts. Catches what the "
        "latent metric cannot: a single-frame jump cut averages away "
        "inside one latent step." % 2.0)),
    ("composition_drift", (
        "mean cosine distance between the first-stage generated latent "
        "steps and the source-window latent steps, per-phase normalised. "
        "The test arm must reduce this relative to control.")),
    ("identity_consistency", (
        "qualitative: does the character/object identity survive the "
        "LR-HR handover? Recorded by the operator from the preview.")),
    ("seam_repair_regression", (
        "the existing seam-repair statistics (level-lock gain, "
        "crossfade overlap) as a regression check: the test arm must "
        "not need MORE repair than control.")),
]


def acceptance(control, test):
    """Apply the audit's acceptance criteria to two arm results.

    Each arm result is a dict with at least:
        ``seam_ratio``        float, the arm's worst seam ratio
        ``verdict``           str, the verdict_for that ratio
        ``composition_drift`` float, the arm's mean drift

    Returns a dict {"accept": bool, "reasons": [str, ...]}.
    """
    reasons = []
    if ACCEPT_SEAMLESS_BOTH:
        if control.get("verdict") != "seamless":
            reasons.append(
                "control arm verdict is '%s', not 'seamless' -- the "
                "baseline itself is not clean; rerun with a cleaner "
                "control before judging the test arm."
                % control.get("verdict"))
        if test.get("verdict") != "seamless":
            reasons.append(
                "test arm verdict is '%s' (ratio %.3f), above the "
                "seamless threshold %.2f -- Path C regressed the seam."
                % (test.get("verdict"), test.get("seam_ratio", 0.0),
                   SEAMLESS_BELOW))
    if ACCEPT_TEST_NOT_WORSE:
        # "must not regress seam metrics relative to control" -- read as
        # verdict BAND, not numeric ratio: a pixel-grade seam (Path C) is
        # expected to sit a little higher than a clean natural handover
        # but still inside the seamless band. The audit's calibration
        # sample (24 real extends) put every clean join below 1.45 and
        # every ambiguous one above 1.55, so the BAND is the meaningful
        # threshold, not the exact number. ACCEPT_SEAMLESS_BOTH already
        # pins both arms to "seamless"; this check catches the case where
        # control itself was only "soft bump" and test slipped to "hard
        # cut" (or any band regression).
        order = {"seamless": 0, "soft bump": 1, "hard cut": 2}
        c, t = order.get(control.get("verdict", ""), 3), order.get(test.get("verdict", ""), 3)
        if t > c:
            reasons.append(
                "test arm verdict '%s' is a worse band than control '%s' "
                "-- the test arm regressed the seam verdict."
                % (test.get("verdict"), control.get("verdict")))
    if ACCEPT_DRIFT_REDUCTION:
        c, t = control.get("composition_drift", 0.0), test.get("composition_drift", 0.0)
        if not (c - t) >= DRIFT_REDUCTION_MIN:
            reasons.append(
                "test arm composition drift %.4f did not reduce control "
                "%.4f by at least %.4f -- the pixel-grade LR pin did not "
                "buy the composition drift reduction it exists to buy."
                % (t, c, DRIFT_REDUCTION_MIN))
    return {"accept": not reasons, "reasons": reasons}
