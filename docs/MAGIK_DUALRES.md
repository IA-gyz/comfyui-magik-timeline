# Dual-resolution pin mode — quick reference

A one-page index. The full design and implementation live in three
canonical places; this card points to them and lists the contract at a
glance.

## Where to read what

| For | Read |
| --- | --- |
| The boundary question (why HR is exact and LR is pixel-grade, the three conversion paths A/B/C, the seam-metric threshold) | `Magik_Timeline_Upstream_Audit.pdf` ch. 11–12 |
| The implementation on `MAGIK_H3MCtxApplyPins`: the R1–R7 table, deliberate deviations from the audit, verification results | [docs/MAGIK_PORT.md §5](MAGIK_PORT.md#5-phase-3-the-dual-resolution-apply-pins-030) |
| The node contract: every input, every output, every mode, what `two_stage` does | [docs/h3-nodes.md — H3 MCtx Apply Pins](h3-nodes.md#h3-mctx-apply-pins) |
| What the suite pins (one record / two representations, shared audio, mask strip, refusals, recipe immunity) | [tests/test_dual_resolution.py](../tests/test_dual_resolution.py) (28 tests) |
| What changes at a join when one side is pixel-grade (seam policy, overlap kept, crossfade) | [tests/test_pixel_grade_seam.py](../tests/test_pixel_grade_seam.py) (12 tests) |

## The contract at a glance

Two nodes, no cycle.

| Node | Field | Notes |
| --- | --- | --- |
| `MAGIK_H3MCtxApplyPins` | in `two_stage` (BOOLEAN, optional) | on = stage 1: pixel-grade pin at this latent's resolution; exact full-res slices go out on `pins`. Off = ordinary single-resolution pinning |
| `MAGIK_H3MCtxApplyPinsStage2` | in `conditioning`, `latent` (the upscaled sampler-1 result), `pins`, `freeze_audio`; optional `masked_audio_feather`, `audio_denoise` | builds stage-2 anchors and mask on the upscaled latent; strips an inherited mask |
| `MAGIK_H3MCtxApplyPinsStage2` | out `conditioning`, `latent`, `pins` | wire sampler 2 from `latent`; `pins` passes through to Trim / Save |

Apply Pins keeps its three outputs (`conditioning`, `latent`, `pins`).

Flow: Apply Pins (`two_stage`) → sampler 1 → latent upscale → Apply Pins
Stage 2 → sampler 2 → Trim / Save.

## The R1–R7 rules

| Rule | One line |
| --- | --- |
| R1 exact HR | The stage-2 pin is the exact sidecar slice, through the unchanged latent slicer; its dimensions must equal the stage-2 target's. |
| R2 pixel LR | The stage-1 pin is the same window re-encoded from the source FILE at the stage-1 resolution (Path C, the audited default). |
| R3 one record | The window is resolved ONCE against the stage-2 target; the stage-1 encode is driven by the resolved record and verified to cover the same file interval. |
| R4 shared audio | One audio slice, one set of timing numbers, both stages. Audio has no spatial axes. |
| R5 per stage | Conditioning and masks are built per stage from the pristine conditioning; a stage-2 target carrying stage 1's mask has it stripped before the fresh HR mask is written. |
| R6 diagnostics | One `DUAL` log line per pin; every refusal names its escapes (resolution mismatch, missing VAEs, stale file hash, through-pins, Path B). |
| R7 recipe untouched | `stage_resolutions` / `stage_grades` live on the resolved pin dict; `serialize_pins` copies named fields only, so the sidecar still records `source_kind: clip`, lineage grade `latent`. |

## Fallback policy

- **Path C (file re-encode)** is the default and the only built path.
  It goes through upstream's existing `_prepare_pixel_pin`, so the LR
  pin is pixel-grade by construction and feeds no path that assumes
  latent exactness.
- **Path B (decode → resize → re-encode)** is selectable but refuses at
  run time. It arrives behind an explicit experimental flag only if the
  Phase 4 acceptance experiment (seam metrics **< 1.45 / 1.9**) shows
  Path C insufficient.
- **Path A (latent downsample)** was rejected by the audit
  (distribution-shift risk + grade washout). Any revival must pass the
  same Phase 4 acceptance as a new "latent-resample" grade level.
- **Through-pins (`via`)** are refused in dual mode: the via rewrite
  points the record at another clip's coordinates, and the file-frame
  mapping needed to verify the LR encode against it is not carried.
  Arrives with the Timeline's dual integration.

## What does NOT change

- No new node class. The 19 node classes from 0.2.0 are unchanged.
- No new wire type. `OBVPM_H3_PINS` / `OBVPM_H3_PINSPECS` /
  `OBVPM_H3_JOINT`, `obvpm_cond_v1`, `mctx_v1` are byte-identical to
  upstream.
- No new API route.
- No frontend wire change. The new input and outputs are auto-discovered
  by ComfyUI from `INPUT_TYPES` / `RETURN_TYPES`; `stage_resolutions` /
  `stage_grades` are runtime wire metadata consumed only by Python
  nodes downstream and never surface in the JS UI.
- The sidecar recipe. A dual-resolution take still saves exactly as a
  single-resolution one: `source_kind: clip`, lineage grade `latent`,
  one row per window.
