# Magik Timeline — port identity map

This pack is a port of **comfyui-obvpm-timeline v0.1.4** (chanon, GPL-3.0)
with the Magik identity applied. Phase 2 is a *behaviour-preserving* port:
every node, route, wire and file format does what upstream does. The only
intended deltas are names and identifiers, listed below, plus one added
environment-variable alias. Everything that other software could depend on
falls in one of two buckets: **renamed** (this pack's own identity) or
**kept byte-identical** (cross-pack and on-disk contracts).

Upstream sources: https://github.com/chanon/comfyui-obvpm-timeline , tag /
release v0.1.4, LICENSE GPL-3.0 (verbatim, kept as `LICENSE`). Every ported
module is upstream 0.1.4 code with the renames below; per-file provenance
is recorded in the banner comment each ported file carries.

## 1. Renamed: this pack's own identity

### 1.1 Node class names (19)

Saved upstream workflows reference class names, so they need a one-time
remap (see §3). Class names were prefixed so both packs can be installed
side by side — ComfyUI keeps one global `NODE_CLASS_MAPPINGS`, and two
packs defining `H3Timeline` would silently overwrite each other.

| Upstream class             | Magik class                    |
| -------------------------- | ------------------------------ |
| `H3Assemble`               | `MAGIK_H3Assemble`             |
| `H3ContextWindows`         | `MAGIK_H3ContextWindows`       |
| `H3JointAudioMask`         | `MAGIK_H3JointAudioMask`       |
| `H3JointConditioning`      | `MAGIK_H3JointConditioning`    |
| `H3JointLatent`            | `MAGIK_H3JointLatent`          |
| `H3JointRender`            | `MAGIK_H3JointRender`          |
| `H3LivePreview`            | `MAGIK_H3LivePreview`          |
| `H3LoadMCtx`               | `MAGIK_H3LoadMCtx`             |
| `H3LoadVideoWithMCtx`      | `MAGIK_H3LoadVideoWithMCtx`    |
| `H3MCtxApplyPins`          | `MAGIK_H3MCtxApplyPins`        |
| `H3MCtxFromFrames`         | `MAGIK_H3MCtxFromFrames`       |
| `H3MCtxPinSpec`            | `MAGIK_H3MCtxPinSpec`          |
| `H3ResultPreview`          | `MAGIK_H3ResultPreview`        |
| `H3SaveMCtxForVideo`       | `MAGIK_H3SaveMCtxForVideo`     |
| `H3SaveVideoWithMCtx`      | `MAGIK_H3SaveVideoWithMCtx`    |
| `H3Timeline`               | `MAGIK_H3Timeline`             |
| `H3TrimAndSaveVideoWithMCtx` | `MAGIK_H3TrimAndSaveVideoWithMCtx` |
| `H3TrimPinned`             | `MAGIK_H3TrimPinned`           |
| `H3VramHeadroom`           | `MAGIK_H3VramHeadroom`         |

Display names keep their upstream wording with the pack suffix changed:
`H3 MCtx Timeline (obvpm)` → `H3 MCtx Timeline (magik)`, so the node search
menu distinguishes the two packs (`_SUFFIX` in `__init__.py`).

### 1.2 HTTP routes (7)

Served by `preview_route.py` on the PromptServer; called by `web/*.js`.
Both sides renamed together.

| Upstream                     | Magik                     |
| ---------------------------- | ------------------------- |
| `POST /obvpm/h3/preview_cut` | `POST /magik/h3/preview_cut` |
| `POST /obvpm/h3/export`      | `POST /magik/h3/export`   |
| `POST /obvpm/h3/clip_meta`   | `POST /magik/h3/clip_meta` |
| `POST /obvpm/h3/seam_levels` | `POST /magik/h3/seam_levels` |
| `POST /obvpm/h3/video_workflow` | `POST /magik/h3/video_workflow` |
| `POST /obvpm/h3/delete_take` | `POST /magik/h3/delete_take` |
| `POST /obvpm/h3/latest_result` | `POST /magik/h3/latest_result` |

### 1.3 Runtime identifiers

| Kind                          | Upstream                    | Magik                    |
| ----------------------------- | --------------------------- | ------------------------ |
| Logger family                 | `obvpm.h3`                  | `magik.h3`               |
| UI events                     | `obvpm.h3.live`, `obvpm.h3.build_progress` | `magik.h3.live`, `magik.h3.build_progress` |
| Frontend extension names      | `obvpm.h3_mctx_drop`, `obvpm.h3_mctx_preview`, `obvpm.h3_save_ui`, `obvpm.h3_mctx_timeline`, `obvpm.h3_mctx_result`, `obvpm.h3_apply_socket_order` | `magik.h3_*` (same tails) |
| Sampler wrapper keys          | `obvpm_h3_joint`, `obvpm_h3_context_windows`, `obvpm_h3_live`, `obvpm_h3_vram_headroom` | `magik_h3_*` |
| Temp file prefixes            | `obvpm_h3_audio_`, `obvpm_h3_join_`, `obvpm_tl_*` | `magik_h3_audio_`, `magik_h3_join_`, `magik_tl_*` |
| Preview/take default names    | `obvpm_h3_preview`, `obvpm_h3_result` | `magik_h3_preview`, `magik_h3_result` |
| Drag MIME                     | `application/x-obvpm-timeline-index` | `application/x-magik-timeline-index` |
| CSS ids / classes             | `obvpm-tl-*`                | `magik-tl-*`             |
| DOM dataset props             | `obvpmFrames`, `obvpmSeam`, `obvpmBaseTitle`, `obvpmGap`, `obvpm_tl_scale` | `magikFrames`, `magikSeam`, `magikBaseTitle`, `magikGap`, `magik_tl_scale` |
| JS globals                    | `__obvpmTlSpace`, `__obvpmRpExec`, `obvpmRPDiag` | `__magikTlSpace`, `__magikRpExec`, `magikRPDiag` |
| localStorage key              | `obvpm.h3.player`           | `magik.h3.player` (stored player prefs start fresh once) |
| Console prefixes              | `[obvpm-h3-*]`, `[obvpm h3]` | `[magik-h3-*]`, `[magik h3]` |
| Example workflow files        | `workflows/h3_obvpm_timeline_*.json` | `workflows/magik_timeline_*.json` |

### 1.4 Environment variables

`nodes_save.py` now accepts `MAGIK_FFMPEG_PATH` first, then falls back to
`OBVPM_FFMPEG_PATH` (upstream's variable, still honoured) and
`VHS_FORCE_FFMPEG_PATH`. Existing setups that already export the upstream
variable keep working.

## 2. Kept byte-identical: cross-pack and on-disk contracts

These carry data across process boundaries or into other packs' code.
Renaming any of them would orphan files or break the companion pack, so
they are upstream strings in Magik too.

| Contract | Value | Why it cannot change |
| --- | --- | --- |
| Wire types (`wiretypes.py`) | `OBVPM_H3_MCTX`, `OBVPM_H3_PINS`, `OBVPM_H3_PINSPECS`, `OBVPM_H3_JOINT` | Link type strings are embedded in saved workflow JSONs; upstream's own docstring records that they were frozen before first release. Clips and wires move freely between upstream and Magik nodes — which is the migration path. |
| Motion-context sidecar format | `mctx_v1`, `.mctx.safetensors` | The durable artifact; every clip saved by upstream must stay loadable (audit rule: the format must not change). |
| Conditioning sidecar format | `obvpm_cond_v1` (`condstore.py`) | Format tag read from saved files. |
| Companion pack API (JS side) | `obvpmRestoreRows`, `obvpmRestoreApply`, `obvpmStaticOutput` | Method names this pack *calls* on nodes the companion pack (comfyui-obvpm) creates. |
| Companion compat checks | `check_obvpm`, `MIN_OBVPM`, problem key `"obvpm"` | The checks target the companion pack by its real name and registry id. |
| Workflow-JSON property names from the companion | `obvpm_layout`, `obvpm_title`, `obvpm_constant`, `obvpm_previous`, `obvpm_group_muted`, `obvpm_list_w`, `obvpm_presets_*`, `obvpm_schema_desc`, `obvpm_override_known`, `(obvpm)` node suffix | Read/written by the companion pack's own JS; the example workflows keep them so those nodes keep working. |

## 3. Migrating a saved upstream workflow

A workflow JSON saved against upstream 0.1.4 stores our old class names in
`nodes[].type` (and inside group-node / subgraph definitions). Links, widget
values and wire types (`OBVPM_H3_*`, `OBVPM_BUNDLE`, companion nodes) are
already correct and must NOT be touched.

**Run the migration script that ships with the pack** (stdlib only; works
on a machine without ComfyUI):

```
python migrate_workflows.py workflow.json more.json saved_workflows/
```

It walks each JSON and remaps any `type` / `class_type` value that is
exactly one of the 19 names in §1.1 (`H3Timeline` → `MAGIK_H3Timeline`).
`class_type` covers the API/prompt format; the recursive walk reaches
group-node definitions whatever key they ride under. The walk is stricter
than a substring replace — a note whose prose mentions `H3Timeline`, or a
group titled `H3 Joint Refine Sampling`, survives untouched — and it is
**idempotent**: an already-Magik workflow comes out `clean` and
byte-identical, so running it twice is a no-op. In-place mode writes a
`.bak` next to each file it changes (`--no-backup` to skip);
`--out DIR` writes remapped copies and leaves the originals alone;
`--reverse` is the exact mirror for rollback. Verified against a shipped
`workflows/*.json` with its prefixes stripped: the migration's output
type list is identical to the shipped file's (848 type strings).

Rollback is symmetrical: uninstall Magik, reinstall upstream 0.1.4, remap
the class names back (`python migrate_workflows.py --reverse …`). Sidecars,
takes and wires written by either pack are readable by the other.

## 4. Test suite

The upstream suite (20 unittest modules + 5 `.mjs` scripts) is ported
unchanged apart from the renames above, plus one hardening fix (the
`LinkedFolders` tearDown now unlinks POSIX symlinks instead of `rmdir`-ing
them, which only worked for Windows junctions). Run it with a ComfyUI
checkout available (`COMFYUI_ROOT`, or a `comfyui`/`ComfyUI` clone next to
this repository):

```
python tests/run_all.py              # unittest discovery, the upstream runner
node tests/test_split.mjs            # each of the 5 .mjs scripts likewise
```

`tests/conftest.py` also bootstraps pytest, but note that with the pack's
`__init__.py` at the repo root, pytest 9's package collection imports the
root as a top-level module named `__init__` and dies on the relative
imports — the unittest runner is the supported way in. The `.mjs` tests
need one dev-time npm dependency (`npm install` once; `package.json` is
declare-only and not shipped to ComfyUI). On a CUDA-less host the harness
forces ComfyUI's `--cpu` path before `comfy.model_management` resolves its
device; on a GPU machine nothing is overridden.

Port-verification record (this checkout): 257 unittest tests → OK
(1 skipped: needs a sibling `comfyui-obvpm` checkout), 5/5 `.mjs` scripts
pass, `py_compile` clean on all pack + test modules, `node --check` clean
on all 4 `web/*.js`, all 5 workflow JSONs parse, smoke import registers
19 `MAGIK_H3*` classes with `(magik)` display names, `./web` web directory,
and the kept contracts verified at runtime: `OBVPM_H3_MCTX` /
`OBVPM_H3_PINS` / `OBVPM_H3_PINSPECS` / `OBVPM_H3_JOINT`, `obvpm_cond_v1`,
`mctx_v1`.

Full-suite record after Phases 3–5 (this checkout): **313 unittest tests →
OK** (1 pre-existing skip; 257 port + 28 dual-resolution + 12
pixel-grade-seam + 13 Phase 4 acceptance + 15 migration), 5/5 `.mjs`
scripts pass, `py_compile` clean on all pack + test modules, `node --check`
clean on all 4 `web/*.js`, all 5 workflow JSONs parse, smoke import
registers 19 `MAGIK_H3*` classes, the kept wire-type / file-format
contracts verified at runtime, and `migrate_workflows.py` verified
end-to-end against a shipped `workflows/*.json` (prefix-stripped then
remapped back: identical type list, 848 strings).

## 5. Phase 3: the dual-resolution Apply Pins (0.3.0)

> **Superseded after 0.4.0.** The one-node form described below (an
> optional `target_latent_hr` input, `lr_encode_source`, appended
> `conditioning_hr` / `latent_hr` outputs) needed the stage-2 latent as an
> input of the node that produces stage 1's latent -- a dependency cycle
> whenever stage 2 starts from stage 1's sampled result. It was replaced by
> two nodes: `MAGIK_H3MCtxApplyPins` with `two_stage` (stage 1) and
> `MAGIK_H3MCtxApplyPinsStage2` (see CHANGELOG, *Latest HEAD*, and
> [h3-nodes.md](h3-nodes.md)). Rules R1-R7 are unchanged; the sections
> below are kept as the 0.3.0 record.

Phase 3 implements the dual-resolution boundary the Phase 1 audit
endorsed (`Magik_Timeline_Upstream_Audit.pdf` ch. 11–12) as **operational
rules R1–R7** on `MAGIK_H3MCtxApplyPins`. No new node class, no new wire
type, no new route: the existing node gains an optional input
(`target_latent_hr`), an enum (`lr_encode_source`, default `file`) and two
appended outputs (`conditioning_hr`, `latent_hr`). Nothing else moves.

### 5.1 What dual mode does

Connect `target_latent_hr` (the stage-2, refine-resolution target) and one
pin window feeds two stages:

| Rule | Implementation |
| --- | --- |
| R1 exact HR | The window is resolved once against the stage-2 target through the unchanged `_prepare_clip_pin`; its dimensions must equal the stage-2 target's (the upstream refusal, naming both). |
| R2 pixel LR | The stage-1 pin is the same window re-encoded from the source FILE at the stage-1 resolution, through the unchanged `_prepare_pixel_pin` (Path C). Every resolved LR record is pixel-grade and feeds no path that assumes latent exactness. |
| R3 one record | The stage-1 encode is DRIVEN by the resolved record — its window length, snapping off — and verified to cover the same file interval; a mismatch refuses, naming both intervals. Nothing re-snaps independently. |
| R4 shared audio | The audio slice (and `audio_steps` / `audio_phase` / `overhang` / `audio_start_step`) is computed once by the exact route and copied onto the LR record. Audio has no spatial axes; one slice serves both stages. |
| R5 per stage | Each stage's conditioning and latent are built from the pristine input conditioning via `_stage_outputs`; the stage-2 target has any inherited noise mask stripped (loudly) before the fresh HR mask is written. Wiring checks cover both latent outputs (slots 1 and 3). |
| R6 diagnostics | One `DUAL` log line per pin names the pin, the raw interval, requested vs actual window, both resolutions, both grades and the shared audio numbers; every refusal names its escapes. |
| R7 recipe untouched | `stage_resolutions` / `stage_grades` live on the resolved pin dict; `serialize_pins` copies named fields only, so the sidecar still records the interval once, `source_kind: clip`, lineage grade `latent`. |

### 5.2 Deliberate deviations from the audit's Table 12-1

- **Output order.** The contract lists `conditioning_hr`, `latent_hr`
  before `pins`; the implementation appends them AFTER `pins`. ComfyUI
  stores links by output slot index, so inserting outputs would re-point
  every saved workflow's `pins` link (slot 2) at `conditioning_hr`.
  Appending keeps slots 0–2 byte-stable; standard mode is unchanged.
- **`lr_encode_source: latents`** is selectable but refuses at run time
  (Path B is not built; it arrives behind an explicit experimental flag
  only if the Phase 4 acceptance experiment shows Path C insufficient) —
  the same offer-then-refuse pattern as `place: at_frame`.
- **Through-pins refused.** A spec carrying `via` (the Timeline's
  prepend/extend-through resolution) is refused in dual mode: the via
  rewrite points the record at another clip's coordinates, and the
  file-frame mapping needed to verify the LR encode against it is not
  carried. Arrives with the Timeline's dual integration.
- **LR resize fit.** The stage-1 encode uses the pixel route's default
  (`cover`), like every other resize in the pack; a spec's own `fit`
  flows through. The stage ratios in the audit's own example differ by
  under 1%, inside the pixel-grade noise floor.
- **Same-file enforcement.** Beyond the audit's text, dual mode verifies
  the source file's hash still matches the sidecar that verified the HR
  slice — the two stages would otherwise pin the same interval of
  different content after a re-encode.

### 5.3 Verification (this checkout)

285 unittest tests → OK (1 skipped, pre-existing: needs a sibling
`comfyui-obvpm` checkout). The 257 upstream-porting tests are unchanged —
the single-resolution path's code now runs through the same
`_split_modes` / `_stage_keyframes` / `_stage_outputs` helpers dual mode
uses, a verbatim extraction the suite pins. The 28 new tests
(`tests/test_dual_resolution.py`) cover: one record / two
representations; the same file interval on both stages; per-stage grades
and resolutions on the wire; the shared audio slice; the stage-2 mask
strip; wiring checks at both latent outputs; guide-mode keyframes at each
stage's resolution; standard-mode compatibility (two trailing `None`s, no
file access, pins identical to a direct prepare); the refusal texts; and
recipe immunity (`serialize_pins` / `lineage_grade` unaffected).
Note for CI on sandboxes where `comfy.model_base` cannot import
(torchvision build mismatch): the fixture pins
`nodes_masked.core_masks_available` True — the checkout itself carries PR
#15375 — and tests the refusal path with it pinned False explicitly.
