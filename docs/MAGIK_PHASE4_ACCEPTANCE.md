# Phase 4 -- acceptance experiment

This is the experiment the audit (ch. 11.7) makes the conditional
boundary of the dual-resolution design: it decides whether the Path C
pixel-grade LR pin (the 0.3.0 default) clears the seam-quality bar, and
so whether the boundary stays as shipped or needs Path B (decode --
resize -- re-encode) as a new ``latent-resample`` grade level.

The protocol is code, not prose. ``phase4_protocol.py`` carries the
arms, the acceptance constants and the ``acceptance()`` function;
``phase4_synthetic.py`` stands in for a real ComfyUI run on the same
``FRAMES_PER_GROUP`` grid; ``phase4_run.py`` is the harness that loads
real takes from two folders and applies the criteria. This doc is the
operator's runbook and the result template -- the source of truth for
the numbers is the code.

## 1. What Phase 4 decides

| Outcome | Consequence |
| --- | --- |
| **PASS** (test arm clears both axes) | The dual-resolution boundary stays as shipped in 0.3.0. Path B (``lr_encode_source: latents``) remains selectable-but-refuses. Path A stays rejected. |
| **FAIL on seam axis** (test arm verdict worse band than control, or test arm not seamless) | Path C regressed the seam. Build Path B (decode -- resize -- re-encode) as a new ``latent-resample`` grade level, behind an explicit experimental flag, and re-run Phase 4 with a ``test_path_b`` arm added. |
| **FAIL on drift axis** (test arm did not reduce composition drift by at least ``DRIFT_REDUCTION_MIN``) | Path C did not buy the composition-drift reduction it exists to buy. Same consequence: build Path B and re-run. |
| **FAIL on both axes** | Path C is the wrong shape. Revisit the audit's ch. 11.4-11.6 before building anything; a Path B that also fails both axes would mean the dual-resolution premise itself needs re-examining. |

A Path A revival (latent downsample) is NOT a Phase 4 outcome. The
audit rejected it (ch. 11.4, distribution-shift risk + grade washout).
If it is ever revived it must pass the SAME Phase 4 acceptance as a new
``latent-resample`` grade level, never as ``latent``-grade, so the five
downstream grade behaviours stay honest.

## 2. The protocol (audit ch. 11.7, verbatim)

### Arms

| Arm | Stage 1 (LR) | Stage 2 (HR) |
| --- | --- | --- |
| **control** | no pin | original HR pins |
| **test_path_c** | LR pixel-grade pin (Path C: source file re-encoded at stage-1 resolution) | HR pin |

A third **ref_path_a** arm is defined in ``phase4_protocol.ARMS`` for a
future Path A revival. It is NOT built and NOT run in this Phase 4; it
exists so a later ``latent-resample`` grade can be measured against the
same bar without re-deriving the protocol.

### Held constant

source clip, pin window, prompt, reference images, seed, step split
(first-stage steps, second-stage steps), upscaler
(``MinimaxH3LatentUpscaler3D`` with ``enable_temporal_chunking`` ON, as
upstream requires -- ``nodes_masked/upscaler_guard`` refuses it off),
target dimensions (stage-1 LR size, stage-2 HR size).

### Measured

| Metric | What | Source |
| --- | --- | --- |
| ``seam_ratio`` | worst step-difference past the boundary as a multiple of the clip's phase-matched median | ``seam_report.measure_seam`` |
| ``cut_scan`` | frame-difference cut scan over the first/last 2 s of the delivered MP4 | ``seam_report.measure_cuts`` |
| ``composition_drift`` | mean ratio deviation of generated-step diffs from the held span's phase-matched baseline | ``phase4_synthetic.composition_drift`` |
| ``identity_consistency`` | qualitative: does the character/object identity survive the LR-HR handover? | operator, from the preview |
| ``seam_repair_regression`` | the existing seam-repair statistics (level-lock gain, crossfade overlap) | ``seam_report.report`` / ``levellock`` |

### Acceptance criteria (``phase4_protocol.acceptance``)

The audit's text: "the test arm must not regress seam metrics relative
to control, AND must measurably reduce first-stage composition drift on
the held-out windows."

| Criterion | Implementation |
| --- | --- |
| ``ACCEPT_SEAMLESS_BOTH`` | both arms must verdict "seamless" (< ``SEAMLESS_BELOW`` = 1.45). The audit's calibration sample (24 real extends) put every clean join below 1.45 and every ambiguous one above 1.55; a "soft bump" is a fail regardless of order. |
| ``ACCEPT_TEST_NOT_WORSE`` | test arm's verdict BAND no worse than control's (``seamless`` < ``soft bump`` < ``hard cut``). Read as band, not numeric ratio: a pixel-grade seam is expected to sit a little higher than a clean handover but stay inside the seamless band. |
| ``ACCEPT_DRIFT_REDUCTION`` | test arm's mean ``composition_drift`` must reduce control's by at least ``DRIFT_REDUCTION_MIN`` = 0.02. The floor is small on purpose: the test arm must show SOME reduction, not necessarily a large one. Calibrate to your own footage. |

## 3. How to run it

### 3.1 Synthetic baseline (no model, no GPU, no folders)

Validates the measurement code and the acceptance logic on a
deterministic latent with the right phase structure. This is what
``tests/test_phase4.py`` pins, and what shipped as
``phase4_result_synthetic_baseline.json`` alongside this doc.

```
cd magik-timeline
python -m phase4_synthetic            # text report
python -m phase4_synthetic --json     # JSON
python -m phase4_run --synthetic      # same, via the harness entry point
```

Expected (and pinned by the test suite):

| arm | seam ratio | verdict | drift |
| --- | --- | --- | --- |
| control | 1.005 | seamless | 0.0804 |
| test_path_c | 1.083 | seamless | 0.0003 |
| ref_hard_cut | 1.915 | hard cut | 0.0003 |

Acceptance: **PASS** (test not worse band than control, both seamless,
drift reduced by 0.0801 >> 0.02). Sanity: **PASS** (ref_hard_cut
verdicts "hard cut" -- the metric CAN detect a real failure).

### 3.2 Real-data run (needs ComfyUI + MiniMax H3 + a generated pair)

1. **Generate the control arm.** In your workflow, leave
   ``two_stage`` OFF on ``MAGIK_H3MCtxApplyPins``. Run
   the workflow for each held-out window. Save the takes to a folder
   ``control_takes/``.

2. **Generate the test arm.** Turn ``two_stage`` ON and add
   ``MAGIK_H3MCtxApplyPinsStage2`` after the latent upscale (Path C,
   the only built path). Hold everything else constant per sec. 2. Save the takes
   to a folder ``test_takes/``.

3. **Run the harness.**

   ```
   cd magik-timeline
   python -m phase4_run \
       --control /path/to/control_takes \
       --test    /path/to/test_takes \
       --out     phase4_result.json
   ```

   Each folder should contain one ``.safetensors`` sidecar per take
   (the ``mctx_v1`` file written by ``MAGIK_H3SaveVideoWithMCtx`` or
   ``MAGIK_H3SaveMCtxForVideo``) plus the matching ``.mp4``. The
   harness measures every extend/prepend take (root clips have no seam)
   and aggregates per arm.

4. **Fill in the qualitative read-outs** (``identity_consistency``,
   ``seam_repair_regression``) in the result JSON from your own
   observation of the previews. The harness does not fill these.

### 3.3 The drift caveat

The audit's drift metric is "first-stage composition drift on the
held-out windows," measured against the SOURCE window's own latent (the
parent's pinned slice). The harness measures it against the TAKE'S OWN
pinned span, which is an approximation: for a take that faithfully
reproduces its pin the two are close; for a take that drifts, the
take's-own-span drift is a LOWER BOUND on the against-source drift.

For the full against-source drift, run ``seam_report.report(folder)``
alongside the harness -- its ``fidelity`` column is the cosine
similarity between the take's pinned span and the parent's slice, when
the parent is in the same folder. A take whose own-span drift reads low
BUT whose fidelity is also low is uninformative: the pin didn't hold,
so the drift metric is measuring noise. Flag those takes in the result
JSON's ``notes`` field and exclude them from the acceptance decision.

## 4. Result template

The harness writes this skeleton to ``--out``; the operator fills in
the qualitative fields and the ``notes`` per take.

```json
{
  "control": {
    "folder": "/path/to/control_takes",
    "take_count": 12,
    "takes": [
      {
        "clip": "clip_00001",
        "relation": "extends",
        "seam_ratio": 1.12,
        "verdict": "seamless",
        "at_frames": 0.0,
        "boundary_ratio": 1.12,
        "composition_drift": 0.0741,
        "cut_scan": {"ratio": 1.3, "verdict": "seamless",
                     "kind": "clean", "at": 0.0},
        "pinned_head": 39, "pinned_tail": 0, "latent_steps": 42,
        "identity_consistency": null,
        "seam_repair_regression": null,
        "notes": ""
      }
    ],
    "seam_ratio": 1.34,
    "verdict": "seamless",
    "composition_drift": 0.0741
  },
  "test": { "...": "same shape as control" },
  "acceptance": {
    "accept": true,
    "reasons": []
  },
  "operator": {
    "name": "",
    "footage": "",
    "seed": "",
    "stage1_resolution": "",
    "stage2_resolution": "",
    "step_split": "",
    "date": ""
  }
}
```

## 5. If Path C fails

1. Do NOT delete Path C. It stays as the 0.3.0 default behind the
   ``lr_encode_source: file`` enum; a real failure is a regression to
   fix, not a revert.
2. Build Path B (``lr_encode_source: latents``): decode the sidecar's
   HR slice, resize to the stage-1 resolution, re-encode through the
   video VAE. Land it behind an explicit experimental flag
   (``MAGIK_H3_ALLOW_PATH_B=1``) so it cannot ship as a silent default.
3. Add a ``test_path_b`` arm to ``phase4_protocol.ARMS`` and re-run
   Phase 4 with all three arms (control, test_path_c, test_path_b).
   Path B must clear the same two-axis acceptance Path C failed.
4. If Path B also fails both axes, the dual-resolution premise itself
   needs re-examining. Re-read audit ch. 11.4-11.6 (the three
   conversion paths and the boundary analysis) before building anything
   else.

## 6. If Path C passes

The boundary stays as shipped in 0.3.0. The ``lr_encode_source:
latents`` enum option stays selectable-but-refuses (the refusal
message names Phase 4 as the gate). Path A stays rejected. Close the
experiment and record the result JSON in the release notes.

## 7. Where to read what

| For | Read |
| --- | --- |
| The boundary question and the three conversion paths A/B/C | ``Magik_Timeline_Upstream_Audit.pdf`` ch. 11.4-11.6 |
| The acceptance criteria text (verbatim) | ``Magik_Timeline_Upstream_Audit.pdf`` ch. 11.7 |
| The protocol as code (arms, constants, ``acceptance()``) | ``phase4_protocol.py`` |
| The synthetic baseline (runnable now, no model) | ``phase4_synthetic.py``, ``phase4_result_synthetic_baseline.json`` |
| The real-data harness | ``phase4_run.py`` |
| What the test suite pins (13 tests) | ``tests/test_phase4.py`` |
| The seam metric itself (thresholds, ``measure_seam``, ``measure_cuts``) | ``seam_report.py`` |
