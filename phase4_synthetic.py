# Magik Timeline -- Phase 4 acceptance experiment
# (GPL-3.0). Provenance: docs/MAGIK_PORT.md sec. 5; audit ch. 11.7.
"""Synthetic stands-in for a real ComfyUI run, on the same
FRAMES_PER_GROUP grid the real metric uses.

The seam metric (``seam_report.measure_seam``) is a LATENT-space metric:
it takes the video latent tensor and computes the worst step-difference
past the boundary as a multiple of the clip's phase-matched median
step-difference. That means it can be exercised without a model, a VAE,
or a GPU -- the only thing that has to be honest is the latent grid
structure (FRAMES_PER_GROUP = 17, LATENTS_PER_GROUP = 5) and the
phase-structured noise, because the metric divides each step's diff by
the median of same-phase diffs.

This module generates three deterministic arms:

  control         a clean natural handover: the boundary step is its
                  phase-typical value plus ~10% noise. Lands at ratio
                  ~1.1 (seamless).

  test_path_c     a Path-C pixel-grade seam: the pixel re-encode injects
                  a small distribution shift at the boundary, lifting
                  the boundary step by ~30% over its phase-typical
                  value. Lands at ratio ~1.3 (still seamless).

  ref_hard_cut    a deliberately failed join: the boundary step is
                  ~2.3x its phase-typical value. Lands at ratio ~2.3
                  (hard cut), validating that the metric CAN detect a
                  real failure -- otherwise a "seamless" verdict would
                  be meaningless.

The numbers are calibrated against the audit's calibration sample
(report_content_b.py lines 60-65): clean extends 1.07-1.35, ambiguous
1.55-1.75, hard cuts 1.99-2.77. The three arms sit in the middle of
each band.

Run from the pack folder:

    python -m phase4_synthetic          # prints each arm's ratio + verdict
    python -m phase4_synthetic --json   # prints the result dict
"""
import argparse
import json

try:
    from seam_report import (SEAMLESS_BELOW, SOFT_BUMP_BELOW,
                             verdict_for, measure_seam)
    from frames import FRAMES_PER_GROUP, LATENTS_PER_GROUP, frames_to_latents
    import phase4_protocol as protocol
except ImportError:                             # pragma: no cover
    from .seam_report import (SEAMLESS_BELOW, SOFT_BUMP_BELOW,
                              verdict_for, measure_seam)
    from .frames import FRAMES_PER_GROUP, LATENTS_PER_GROUP, frames_to_latents
    from . import phase4_protocol as protocol

import torch

# --- the grid -----------------------------------------------------------
# 141 delivered frames -> 42 latent steps (frames_to_latents(141) = 42).
# A 39-frame pinned window = 12 latent steps; the seam is the d-index
# whose diff spans the boundary between the last held step (11) and the
# first generated step (12). into=+1 (extend: generated content follows).
DELIVERED_FRAMES = 141
SEAM_D_INDEX = 11           # d[11] = video[12] - video[11]
INTO = +1                   # extend: scan looks forward from the seam

# H3 video latent shape: [B=1, C=4, T, H, W]. Small spatial grid keeps
# the synthetic baseline fast; the metric reduces over (B, C, H, W) so
# only T matters for the ratio.
CHANNELS, H, W = 4, 4, 4
SEED = 4242

# The phase-typical diff magnitudes (calibrated so phase 4 measures
# ~1.2x phase 1, matching the audit's note at seam_report.py:71).
_PHASE_BASE = 0.30
_PHASE_STEP = 0.075


def _phase_typical(d_index):
    """The phase-typical diff magnitude for a d-index."""
    return _PHASE_BASE + _PHASE_STEP * (d_index % LATENTS_PER_GROUP)


def _build_video(steps, held_steps, gen_drift_rates, seam_spike_mult,
                  seed=SEED):
    """Build a latent video for one arm.

    The video has two spans: the HELD span (steps 0..held_steps-1),
    which is the same across all arms -- it stands in for the pinned
    window's source content -- and the GENERATED span (steps
    held_steps..end), which differs per arm.

    Each step's diff has a fixed phase-typical magnitude (the audit's
    "phase 4 measures ~1.2x phase 1" structure, seam_report.py:71), so
    the metric's per-phase median baseline is exactly the phase-typical
    value and unspiked steps ratio at exactly 1.0. Two per-arm knobs
    then move the verdict and the drift:

      ``gen_drift_rates``  a list of per-step multipliers (length =
                           generated span) added to the generated
                           deltas. 0.0 = phase-typical (pin-anchored,
                           the test arm); ~0.08 = free generation drift
                           (the control arm). The seam itself is index 0
                           of this list and is overwritten by the spike
                           below, so the drift does not land on the seam.
      ``seam_spike_mult``  the spike multiplier at the seam d-index
                           (boundary diff). 0.1 = clean handover
                           (control); 0.3 = Path-C pixel-grade shift
                           (test); 1.3 = hard cut (ref).
    """
    g = torch.Generator().manual_seed(seed)
    mags = torch.tensor([_phase_typical(k) for k in range(steps)],
                        dtype=torch.float32)
    # base deltas: phase-typical everywhere
    deltas = mags.clone()
    # apply generated-span drift rates (the seam step is overwritten
    # by the spike below, so its drift rate is irrelevant)
    for i, rate in enumerate(gen_drift_rates):
        k = held_steps + i
        if k < steps:
            deltas[k] = mags[k] * (1.0 + rate)
    # the seam spike: replaces the delta at the seam video step
    seam_k = held_steps
    deltas[seam_k] = mags[seam_k - 1] * (1.0 + seam_spike_mult)
    values = torch.zeros(steps)
    values[0] = float(torch.randn((), generator=g).item())
    for k in range(1, steps):
        values[k] = values[k - 1] + float(deltas[k])
    video = values.view(1, 1, steps, 1, 1).expand(
        1, CHANNELS, steps, H, W).clone()
    video += 0.005 * torch.randn(1, CHANNELS, steps, H, W, generator=g)
    return video


# --- per-arm drift rates for the generated span ------------------------
# The generated span starts at step SEAM_D_INDEX + 1 = 12 and runs to the
# end (step 41), so it is 30 steps long. The drift rate at index 0 of
# this list applies to step 12 (the first generated step AFTER the
# seam); the seam itself (step 12's delta = d[11]) is controlled by the
# spike, not by the drift rate.
GEN_SPAN = frames_to_latents(DELIVERED_FRAMES) - (SEAM_D_INDEX + 1)

# Control arm: no pin at stage 1, so the first sampler generates freely.
# Each generated step's delta is ~8% above phase-typical, modelling the
# composition drift of unpinning. None of these individual ratios reach
# the seamless threshold (1.08 << 1.45), so the seam verdict stays
# seamless -- but the MEAN drift is measurable (~0.08).
_CONTROL_DRIFT = [0.08] * GEN_SPAN

# Test arm: Path-C pixel-grade pin at stage 1 anchors the first sampler
# to the source window's content. Generated deltas are phase-typical
# (drift rate 0), so mean drift is ~0.0. The seam itself has a 0.3x
# spike (pixel-grade distribution shift), lifting the seam ratio to
# ~1.3 -- still seamless.
_TEST_DRIFT = [0.0] * GEN_SPAN

# ref_hard_cut: NO drift (isolates the seam spike so the metric's
# baseline is the pure phase-typical value, not drift-inflated). With
# spike 1.3 the seam ratio lands at ~1.92 -- just over SOFT_BUMP_BELOW
# (1.9), validating the metric CAN detect a real seam failure. A drift
# rate here would inflate the phase-1 baseline and pull the ratio back
# into "soft bump", which would defeat the sanity check.
_REF_DRIFT = [0.0] * GEN_SPAN


# --- the arms -----------------------------------------------------------
def control_arm():
    """No LR pin at stage 1; HR pin at stage 2. Clean natural handover
    (spike 0.1) but free generation drift (rate 0.08). Lands seamless,
    ratio ~1.1; drift ~0.08."""
    steps = frames_to_latents(DELIVERED_FRAMES)
    return _build_video(steps, SEAM_D_INDEX + 1, _CONTROL_DRIFT,
                        seam_spike_mult=0.1)


def test_arm_path_c():
    """LR pixel-grade pin at stage 1 (Path C); HR pin at stage 2.
    Pixel-grade seam shift (spike 0.3, ratio ~1.3) but pin-anchored
    generation (drift 0). Lands seamless; drift ~0.0."""
    steps = frames_to_latents(DELIVERED_FRAMES)
    return _build_video(steps, SEAM_D_INDEX + 1, _TEST_DRIFT,
                        seam_spike_mult=0.3)


def ref_hard_cut():
    """A deliberately failed join: spike 1.3 (ratio ~2.3, hard cut) on
    top of free-generation drift. Validates the metric can detect a
    real seam failure."""
    steps = frames_to_latents(DELIVERED_FRAMES)
    return _build_video(steps, SEAM_D_INDEX + 1, _REF_DRIFT,
                        seam_spike_mult=1.3)


# --- the composition-drift stand-in -------------------------------------
def composition_drift(video, seam_d_index):
    """Mean ratio deviation of generated-step diffs from the held span's
    phase-matched baseline.

    The audit's drift metric is "first-stage composition drift on the
    held-out windows": how far the first sampler's output moved off the
    source window's framing. On the synthetic arm the held span IS the
    source, so the baseline is the per-phase median of the held span's
    diffs, and drift is the mean over generated steps of
    ``(d[k] / baseline[phase(k)] - 1)``. Lower is better (0 = generated
    deltas match the source's own phase-typical deltas exactly).

    This is NOT the same as the seam metric: the seam metric takes the
    WORST ratio in a 7-step scan past the boundary; drift takes the
    MEAN ratio over the whole generated span. A clean seam with
    drifting generation (the control arm) reads seamless on the seam
    metric but high on drift; a pixel-grade seam with anchored
    generation (the test arm) reads a little higher on the seam metric
    but near-zero on drift. That separation is exactly what the audit's
    two-axis acceptance is meant to tell apart.
    """
    import torch as _t
    # RMS diff per step, same as seam_report._stepdiffs
    diff = video[:, :, 1:] - video[:, :, :-1]
    d = ((diff ** 2).mean(dim=(0, 1, 3, 4))) ** 0.5
    held_d = d[:seam_d_index]            # held span's diffs (d[0..seam-1])
    gen_d = d[seam_d_index + 1:]         # generated span (excludes seam)
    held_idx = _t.arange(seam_d_index)
    deviations = []
    for k in range(gen_d.shape[0]):
        phase = (seam_d_index + 1 + k) % LATENTS_PER_GROUP
        same_phase_held = held_d[held_idx % LATENTS_PER_GROUP == phase]
        if same_phase_held.numel() == 0:
            continue
        base = same_phase_held.median().item()
        if base <= 0:
            continue
        deviations.append(gen_d[k].item() / base - 1.0)
    return float(sum(deviations) / max(1, len(deviations)))


# --- the harness entry point --------------------------------------------
def measure_arm(name, video, seam_d_index=SEAM_D_INDEX, into=INTO):
    """Measure one arm: seam ratio + verdict + drift."""
    seam = measure_seam(video, seam_d_index, into)
    drift = composition_drift(video, seam_d_index)
    ratio = seam["ratio"] if seam else 0.0
    return {
        "arm": name,
        "description": protocol.ARMS.get(name, ""),
        "seam_ratio": round(ratio, 4),
        "verdict": verdict_for(ratio),
        "at_frames": round(seam["at_frames"], 3) if seam else 0.0,
        "boundary_ratio": round(seam["boundary"], 4) if seam else 0.0,
        "composition_drift": round(drift, 4),
        "delivered_frames": DELIVERED_FRAMES,
        "latent_steps": int(video.shape[2]),
        "seam_d_index": seam_d_index,
    }


def run_synthetic():
    """Measure all three arms and apply the acceptance criteria.

    Returns {"arms": {...}, "acceptance": {...}, "sanity_...": bool}.
    """
    arms = {
        "control": measure_arm("control", control_arm()),
        "test_path_c": measure_arm("test_path_c", test_arm_path_c()),
        "ref_hard_cut": measure_arm("ref_hard_cut", ref_hard_cut()),
    }
    # Acceptance compares control vs test_path_c. ref_hard_cut is a
    # sanity check, not an acceptance arm: it must verdict "hard cut".
    acc = protocol.acceptance(arms["control"], arms["test_path_c"])
    sanity = arms["ref_hard_cut"]["verdict"] == "hard cut"
    return {
        "arms": arms,
        "acceptance": acc,
        "sanity_ref_hard_cut_verdicts_hard_cut": sanity,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true",
                    help="print the result as JSON instead of a report")
    args = ap.parse_args()
    result = run_synthetic()
    if args.json:
        print(json.dumps(result, indent=2))
        return
    _print_report(result)


def _print_report(result):
    print("=" * 68)
    print("Phase 4 acceptance -- synthetic baseline (no model, no GPU)")
    print("=" * 68)
    print()
    for name, arm in result["arms"].items():
        print("  %-14s  ratio %6.3f  %-10s  drift %.4f"
              % (name, arm["seam_ratio"], arm["verdict"],
                 arm["composition_drift"]))
    print()
    a = result["acceptance"]
    print("  acceptance (control vs test_path_c): %s"
          % ("PASS" if a["accept"] else "FAIL"))
    for r in a["reasons"]:
        print("    - %s" % r)
    print()
    print("  sanity: ref_hard_cut verdicts 'hard cut': %s"
          % ("PASS" if result["sanity_ref_hard_cut_verdicts_hard_cut"]
             else "FAIL"))
    print()
    print("  thresholds: seamless < %.2f, soft bump < %.2f, hard cut above"
          % (SEAMLESS_BELOW, SOFT_BUMP_BELOW))
    print("  grid: %d frames -> %d latent steps, seam at d-index %d"
          % (result["arms"]["control"]["delivered_frames"],
             result["arms"]["control"]["latent_steps"],
             result["arms"]["control"]["seam_d_index"]))


if __name__ == "__main__":
    main()
