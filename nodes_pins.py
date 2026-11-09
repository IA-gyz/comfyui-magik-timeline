# Magik Timeline -- ported from comfyui-obvpm-timeline v0.1.4
# (GPL-3.0). Provenance and identity mapping: docs/MAGIK_PORT.md
"""The pins pipeline: decide -> slice+execute.

  MAGIK_H3MCtxPinSpec     spec builder: pure data, no tensor work
  MAGIK_H3MCtxApplyPins   slices (sole frame<->latent math site, via
                    _prepare_pins) and attaches pinned runs as native
                    `minimax_keyframes` entries; outputs the resolved
                    pins wire. With two_stage on it is STAGE 1 of the
                    dual-resolution flow: one shared temporal record,
                    a pixel-grade pin at this latent's size, the exact
                    sidecar slice left on the pins wire (docs/
                    MAGIK_PORT.md §5, audit rules R1-R7)
  MAGIK_H3MCtxApplyPinsStage2
                    STAGE 2: pins + the upscaled sampled latent -> the
                    exact slice anchored/masked at full resolution
  MAGIK_H3TrimPinned      removes pinned scaffolding from the decoded output

Phase 1 scope: a single `before` pin sourced from a clip's tail -- the
extend case. The wire formats (PINSPECS, PINS) already carry the general
shape (placement, multiple pins, image sources) so later phases add
capability without changing these interfaces.
"""

import logging
import math

import node_helpers

from . import frames as fr
from . import mctx
from . import nodes_masked
from . import wiretypes as wt
from .avpack import unpack_av

_LOG = logging.getLogger("magik.h3")

# Marks a `minimax_keyframes` entry this node appended for a LINEAGE pin,
# as opposed to a CONTENT keyframe some other node anchored (an image at
# frame 60, a stock first/last frame, an AddGuide chain).
#
# The distinction is not cosmetic. Conditioning is per-SHOT and travels
# with the clip; a lineage pin is per-POSITION and is rebuilt by whatever
# walks the timeline, at whatever resolution that pass runs. Persisting a
# lineage keyframe would hand a later pass a second, stale anchor for a
# window it is about to pin itself -- at the wrong resolution, since these
# latents are the generation's size.
#
# Core reads only `latent` / `audio_latent` / `resolved_frame_index` from
# each entry (model_base.py), so an extra key rides along untouched.
# Video and audio are separate ENTRIES, so one tag per entry covers both
# streams with no special case.
PIN_ORIGIN = "pin"


def _keyframe_origin(pin):
    """PIN_ORIGIN for a lineage pin, None for a content one.

    DERIVED from the spec, never asserted by this node. Everything that
    reaches here today continues from another clip, but the same wire is
    the obvious road for a CONTENT anchor -- an image the timeline holds
    at frame 60, a sound cue -- and such a pin's keyframe has to survive
    into the `.cond`, because it is part of what the shot IS.

    The test is the one mctx.lineage_grade already uses for the header:
    a lineage edge is a known source kind plus a source id. A content
    pin will arrive with neither and go untagged, which the save side
    reads as content without having to know it exists.
    """
    # A RESOLVED pin (what apply() holds) names its source as `kind` +
    # `source_id` at the top level and keeps the recipe's `source_kind`
    # inside `spec`; a bare spec has only the latter. Reading just one
    # spelling tagged nothing for months: every guided and both-mode
    # keyframe reached the .cond as "content", which a same-resolution
    # refine tolerates and an upscaled one cannot lay out (a 34x60
    # keyframe on a 68x120 target grid).
    spec = pin.get("spec") or {}
    kind = pin.get("source_kind") or spec.get("source_kind") or pin.get("kind")
    source_id = pin.get("source_id") or spec.get("source_id")
    if kind in mctx.LINEAGE_KINDS and source_id:
        return PIN_ORIGIN
    return None


def _ensure_native_anchors():
    """Refuse unless live core natively places pins where we need them.

    Core merged arbitrary-position, multi-frame keyframe anchoring
    (PR #15439): PackedLayout places every keyframe at
    `cursor + FRAME_RESCALE * resolved_frame_index`, with the cursor past
    the reference spans, and extra_conds concatenates keyframe and
    reference latents. That is the whole mechanism this pack used to
    carry as runtime patches; pins now ride plain `minimax_keyframes`.

    Checked here, on first apply, because the failure modes are silent:
    an OLDER core hard-rejects interior anchors only at sampling time
    (or, worse, a foreign pack's patch places them by different rules).
    The new signature dropped `frame_count`, so its absence plus the
    `keyframes` parameter identifies a native core; a wrapped or
    replaced constructor identifies a foreign patcher (H3Studio,
    MMH3Tools, the Motion-Context lineage) whose coordinate rules we
    must not build on.
    """
    import inspect
    import comfy.ldm.minimax.model as _mm
    cls = getattr(_mm, "PackedLayout", None)
    init = getattr(cls, "__init__", None)
    cls_mod = getattr(cls, "__module__", "")
    if cls_mod and not cls_mod.startswith("comfy."):
        raise RuntimeError(
            "magik.h3: another pack has REPLACED H3's PackedLayout class "
            "(it now comes from %r -- ComfyUI-H3Studio does this). The two "
            "continuation mechanisms cannot coexist in one session; disable "
            "one pack and restart." % cls_mod)
    if hasattr(init, "__wrapped__") or getattr(init, "__module__", cls_mod) != cls_mod:
        raise RuntimeError(
            "magik.h3: another pack has patched H3's PackedLayout "
            "constructor (it now comes from %r). This pack pins through "
            "core's own keyframe anchoring and cannot run under a foreign "
            "layout patch; disable that pack and restart."
            % getattr(init, "__module__", "?"))
    try:
        params = tuple(inspect.signature(init).parameters)
    except (TypeError, ValueError):
        params = ()
    if "keyframes" not in params or "frame_count" in params:
        raise RuntimeError(
            "magik.h3: this ComfyUI predates native arbitrary-position "
            "keyframe anchors for MiniMax H3 (PR #15439, merged 2026-08-13). "
            "Update ComfyUI to use the mctx pin nodes.")


def pins_trim_totals(pins):
    """(head, tail) frames of pinned scaffolding, from PREPARED pins.

    Authoritative by construction: `covered` is what Prepare actually
    sliced (snap_down may have shrunk a requested window), so trim and
    save must read these, never the original spec numbers.
    """
    head = tail = 0
    for p in (pins or []):
        covered = int(p.get("covered", 0))
        spec = p.get("spec") or {}
        shape = (int(spec.get("mask_ramp_frames", 0) or 0),
                 float(spec.get("mask_ramp_edge", 0.0) or 0.0),
                 float(spec.get("mask_hold", 0.0) or 0.0))
        # A softly-held window is trimmed only as far as it is HELD --
        # the ramped frames are this take's own work, matched to what it
        # generated, so they ship and the source enters past them
        # (nodes_masked.handover_frames).
        # ...and a hard-held one hands over a step inside its edge when
        # the recipe says so (handover_edge): the older clip's frames
        # there are its decode edge, this take's are mid-file.
        edge = bool(spec.get("handover_edge"))
        if p.get("place") == "before":
            head += nodes_masked.handover_frames(covered, "before", *shape,
                                                 decode_edge=edge)
        elif p.get("place") == "after":
            tail += covered - nodes_masked.handover_frames(
                covered, "after", *shape, decode_edge=edge)
    return head, tail


class MAGIK_H3MCtxPinSpec:
    CATEGORY = "magik/h3"
    FUNCTION = "build"
    RETURN_TYPES = (wt.PINSPECS,)
    RETURN_NAMES = ("pin_specs",)
    DESCRIPTION = (
        "Describes a pin as pure data: which part of a source clip to "
        "carry into the next generation, and where it sits in the target. "
        "No tensor work happens here -- Apply slices later. Chain "
        "several to pin from several sources."
    )
    OUTPUT_TOOLTIPS = (
        "The upstream stack plus this pin's spec. Feed MAGIK_H3MCtxApplyPins; "
        "Trim/Save ride the resolved pins wire Apply produces.",
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "mctx": (wt.MCTX, {
                    "tooltip": "The source clip's latents+lineage bundle "
                               "from MAGIK_H3LoadVideoWithMCtx."}),
                "window": (list(fr.WINDOW_CHOICES), {
                    "default": "39",
                    "tooltip": "Frames of the source to pin. Only these "
                               "lengths are whole latent steps. 5 is barely "
                               "fluid, 22 nearly seamless; longer windows pin "
                               "more motion but cost more of the new clip. "
                               "1 is reserved for the future keyframe path."}),
                "take_from": (["head", "tail", "at_frame"], {
                    "default": "tail",
                    "tooltip": "Which part of the source supplies the "
                               "window: tail = the end (extend), head = the "
                               "start (for future prepends), at_frame = the "
                               "window ENDING at take_from_frame (a "
                               "timeline cut). Latent-grade cuts must land "
                               "on the 17-frame grid; misaligned cuts are "
                               "refused with the nearest valid frames."}),
                "take_from_frame": ("INT", {
                    "default": 0, "min": 0, "max": 4096,
                    "tooltip": "Only used when take_from is at_frame: the "
                               "delivered frame the window ends at (your "
                               "cut point)."}),
                "place": (["before", "after", "at_frame"], {
                    "default": "before",
                    "tooltip": "Where the pinned run sits in the target: "
                               "before = context leading in (extend; "
                               "re-rendered at the head, trimmed). after = "
                               "context leading out (prepend; generation "
                               "runs toward it, re-rendered at the tail, "
                               "trimmed -- typically with take_from=head). "
                               "at_frame (kept interior content) is NOT "
                               "YET IMPLEMENTED."}),
                "place_at_frame": ("INT", {
                    "default": 0, "min": 0, "max": 4096,
                    "tooltip": "Only used when place is at_frame (not yet "
                               "implemented)."}),
                "audio_window": ("INT", {
                    "default": 0, "min": 0, "max": 240,
                    "tooltip": "Frames of tail audio to pin, end-aligned with "
                               "the video window. 0 follows the video "
                               "window."}),
                "mode": (["masked", "guide", "both"], {
                    "default": "masked",
                    "tooltip": "guide: the window rides as native keyframe "
                               "cond rows and the model RE-RENDERS it "
                               "(trimmed after; any ladder window). "
                               "masked: the window's latents are written "
                               "INTO the target and preserved with a "
                               "denoise mask -- one rendering, no level "
                               "step, audio kept verbatim. Needs ComfyUI "
                               ">= 2026-08-18 and a window on the shared "
                               "AV grid (39/90/141/...). both: the same "
                               "window as BOTH -- the keyframe rows give "
                               "the model something to steer toward, the "
                               "mask makes the arrival exact. They agree "
                               "by construction (one slice, one source), "
                               "so they reinforce rather than fight; the "
                               "cost is the extra cond tokens."}),
                "mask_ramp_frames": ("INT", {
                    "default": 0, "min": 0, "max": 256,
                    "tooltip": "Masked/both only. Softens the hold over "
                               "this many frames measured back from the "
                               "JOIN -- the window edge facing the "
                               "delivered content (an after-pin's first "
                               "frame, a before-pin's last). 0 = the "
                               "whole window is held at mask_hold. The "
                               "mask is a strength, not a flag: core "
                               "runs a row at sigma = mask * sigma, so "
                               "0 preserves verbatim, 1 generates "
                               "freely, and between them the model is "
                               "told how much of the destination is "
                               "already there. The softened frames are "
                               "DELIVERED rather than trimmed (the "
                               "source enters past them), so this is "
                               "the run's remaining runway to arrive "
                               "in. Prefer a value one frame PAST a "
                               "latent step offset -- 6/10/14/19/23 -- "
                               "since a ramp landing exactly on an "
                               "offset (5/9/13/17/22) zeroes that step "
                               "and loses it from the runway."}),
                "mask_ramp_edge": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 1.0, "step": 0.05,
                    "tooltip": "The mask value AT the join, falling to "
                               "mask_hold across mask_ramp_frames. Raise "
                               "it to give the model room to bend into "
                               "the destination (a landing strip); leave "
                               "it 0 to keep the join exact. Ignored "
                               "when mask_ramp_frames is 0."}),
                "mask_hold": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 1.0, "step": 0.05,
                    "tooltip": "The mask value through the rest of the "
                               "window. 0 = verbatim (the default, and "
                               "what makes a masked extend seamless). "
                               "Raising it with mask_ramp_frames at 0 "
                               "holds the WHOLE window softly, so the "
                               "pinned rows join the flow instead of "
                               "standing as a fixed wall."}),
            },
            "optional": {
                "pin_specs": (wt.PINSPECS, {
                    "tooltip": "Upstream pin stack to append to."}),
            },
        }

    def build(self, mctx, window, take_from, take_from_frame, place,
              place_at_frame, audio_window, mode="masked",
              mask_ramp_frames=0, mask_ramp_edge=0.0, mask_hold=0.0,
              pin_specs=None):
        if mctx is None:
            raise ValueError(
                "MAGIK_H3MCtxPinSpec: mctx is None -- the loaded clip has no "
                "verified sidecar, so there are no latents to pin. Use the "
                "pixel route (H3 MCtx From Frames) or re-save the "
                "take with MAGIK_H3SaveVideoWithMCtx.")
        meta = mctx["meta"]
        delivered = int(meta.get("delivered_frames", 0))
        w = int(window)
        if take_from == "tail":
            start = delivered - w
        elif take_from == "head":
            start = 0
        else:
            start = int(take_from_frame) - w
        spec = {
            "source": mctx,
            "source_id": mctx.get("self_id", ""),
            "source_kind": "clip",
            "take_from": take_from,
            "take_from_frame": int(take_from_frame),
            "requested_window": w,
            # resolved best-effort range; Apply re-derives under its snap
            # policy and is authoritative
            "source_start": max(0, start),
            "source_frames": min(w, delivered) if delivered else w,
            "place": place,
            "place_at_frame": int(place_at_frame),
            "audio_window": int(audio_window),
            "mode": mode,
            # Only meaningful for a mode that masks; carried regardless
            # so a spec stays a faithful record of what was asked for.
            "mask_ramp_frames": int(mask_ramp_frames),
            "mask_ramp_edge": float(mask_ramp_edge),
            "mask_hold": float(mask_hold),
            # A hard-held window hands over a step inside the older
            # clip's decode edge (nodes_masked.handover_frames). Latent
            # grade only: these held frames decode from the very latents
            # the older clip decoded, so only its edge frames differ.
            "handover_edge": mode in ("masked", "both"),
        }
        # Where the parent was when it was pinned, as pixel pins record
        # it. The hash (source_id) still decides what the parent IS; this
        # only lets it be FOUND again when it does not sit in the take's
        # own folder (Result Preview scans that folder first) -- a clip
        # added from outside the project folder. Recorded only for a
        # bundle loaded from a file: one arriving on a wire from this
        # run has no location yet.
        clip = mctx.get("clip")
        if clip:
            spec["source_path"] = str(clip).replace("\\", "/")
        return (list(pin_specs or []) + [spec],)


def _prepare_pins(pin_specs, snap_window_down_to_available, ctx=None):
    """Materialize specs into sliced pins. THE sole slicing site.

    Lives as a module function (not a node) since 2026-08-12: with Save
    and Trim consuming the resolved pins wire, a separate Prepare node
    added a mandatory hop with no unique contribution -- slicing is cheap
    and Apply outputs the pins itself. The single-implementation-site
    principle holds here regardless of node shape.

    `ctx` carries what a PIXEL source needs and a latent one does not:
    the VAEs and the target resolution. Absent = latent sources only.
    """
    if not pin_specs:
        raise ValueError("MAGIK_H3MCtxApplyPins: the pin_specs stack is empty.")
    pins = []
    for i, spec in enumerate(pin_specs):
        kind = spec.get("source_kind")
        if kind == "clip":
            pin = _prepare_clip_pin(i, spec, snap_window_down_to_available)
        elif kind == "clip_pixels":
            pin = _prepare_pixel_pin(i, spec, snap_window_down_to_available,
                                     ctx)
        else:
            raise ValueError(
                "MAGIK_H3MCtxApplyPins: spec %d has source_kind %r; known kinds "
                "are 'clip' (latents from a sidecar) and 'clip_pixels' "
                "(a file, VAE-encoded)." % (i, kind))
        pins.append(_seen_from(pin, spec))
    return pins


def _seen_from(pin, spec):
    """A pin sliced from the take that continues the clip it was asked of
    (nodes_load.prepend_through), recorded as the clip's.

    The window is the clip's frames up to that take's join and the
    take's own after it -- one latent, held in the take. The recipe names
    the clip, in its coordinates, so the new take leads into the clip at
    the cut it was queued from; its window simply runs on past the
    clip's end, into what the timeline plays after it. Both routes: a
    latent pin slices the take's latent (raw frames), a pixel pin decodes
    the take's file (file frames)."""
    via = spec.get("via")
    if not via:
        return pin
    s = {k: v for k, v in (pin.get("spec") or {}).items() if k != "via"}
    if s.get("source_kind") == "clip_pixels":
        s["source_start"] = int(s.get("source_start", 0)) + via["file_offset"]
    else:
        s["source_start"] = int(s.get("source_start", 0)) + via["raw_offset"]
        if s.get("audio_start_step") is not None:
            if via.get("audio_offset") is None:
                # placed by the rule an older recipe is placed by
                s.pop("audio_start_step", None)
                s.pop("audio_steps", None)
            else:
                s["audio_start_step"] = (int(s["audio_start_step"])
                                         + via["audio_offset"])
    s["source_id"] = via["source_id"]
    if via.get("source_path"):
        s["source_path"] = via["source_path"]
    else:
        s.pop("source_path", None)
    return dict(pin, spec=s, source_id=via["source_id"])


def _lineage_pin(header, place):
    """The take's one pin from a clip on that side ("before": the clip it
    continues, "after": the clip it leads into), or None."""
    pins = [s for s in mctx.parse_pins(header)
            if s.get("place") == place and s.get("source_id")
            and s.get("source_kind") in mctx.LINEAGE_KINDS]
    return pins[0] if len(pins) == 1 else None


def _lineage_file(path, p):
    """The file of the clip pin `p` of the take at `path` names, found as
    the Result Preview finds it: by sidecar id in the take's folder, then
    the path the recipe recorded -- and the hash still decides what that
    clip is. None when it is gone."""
    import os

    from . import nodes_load

    pid = p["source_id"]
    ap = os.path.abspath(path)
    side = mctx.scan_for_parent(os.path.dirname(ap), pid)
    if side:
        cand = side[:-len(mctx.SIDECAR_SUFFIX)] + ".mp4"
        if os.path.isfile(cand):
            return cand
    if p.get("source_path"):
        try:
            cand = nodes_load.resolve_clip_path(p["source_path"])
        except Exception:
            cand = None
        if cand and os.path.isfile(cand) \
                and nodes_load._cached_hash(cand) == pid:
            return cand
    return None


def _file_frame(p, raw, fpath):
    """The frame of the clip pin `p` names, in its FILE, under its frame
    `raw` in the recipe's coordinates: a latent recipe counts the clip's
    raw frames (its own held head first), a pixel recipe its file's."""
    if p.get("source_kind") != "clip":
        return raw
    try:
        h = mctx.read_header(mctx.sidecar_path(fpath))
    except (OSError, ValueError):
        return None
    return raw - int(h.get("pinned_head_frames", 0) or 0)


def _file_frames(fpath):
    """How many frames the file holds: its sidecar's delivered count, or
    the file itself when it has none."""
    try:
        h = mctx.read_header(mctx.sidecar_path(fpath))
        return int(h.get("delivered_frames", 0) or 0)
    except (OSError, ValueError):
        from . import nodes_encode as ne
        return int(ne.probe_clip(fpath)["frames"])


def _parent_frames(path, header, k):
    """Where the take at `path` continues from, as (parent file, the
    parent's frame under the take's delivered frame 0), when the parent's
    file holds the `k` frames before that; else None.

    A take's held head IS the frames of the clip it continues: its raw
    frame r is that clip's raw frame `source_start + r` (a latent recipe;
    a pixel recipe counts the parent file's own frames). Its delivered
    frame 0 is raw frame `pinned_head`.
    """
    ph = int(header.get("pinned_head_frames", 0) or 0)
    p = _lineage_pin(header, "before")
    if ph < k or p is None:
        return None
    ppath = _lineage_file(path, p)
    if ppath is None:
        _LOG.warning("magik.h3: %s is too short for its window, and the "
                     "clip it continues (%s) was not found to read the "
                     "frames before it from", path, p["source_id"][:12])
        return None
    p0 = _file_frame(p, int(p.get("source_start", 0) or 0) + ph, ppath)
    if p0 is None:
        return None
    if p0 < k:
        _LOG.warning("magik.h3: %s's window would reach %d frames back, "
                     "past the start of the clip it continues (%s)",
                     path, k, ppath)
        return None
    return ppath, p0


def _child_frames(path, header, k):
    """The other way round, for a take made by prepending: (the file of
    the clip it leads into, that file's frame right after the take's
    last), when that file holds the `k` frames from there; else None.

    A prepend-made take's held tail IS the frames of the clip it leads
    into: the last `source_frames` of its raw frames are that clip's raw
    `source_start` on. The frames a soft or edge hold handed over sit
    before the tail, delivered, so the take's file ends at that clip's
    raw `source_start + source_frames - pinned_tail` -- where the
    recipe's join is.
    """
    pt = int(header.get("pinned_tail_frames", 0) or 0)
    p = _lineage_pin(header, "after")
    if pt < k or p is None:
        return None
    cpath = _lineage_file(path, p)
    if cpath is None:
        _LOG.warning("magik.h3: %s is too short for its window, and the "
                     "clip it leads into (%s) was not found to read the "
                     "frames after it from", path, p["source_id"][:12])
        return None
    q0 = _file_frame(p, int(p.get("source_start", 0) or 0)
                     + int(p.get("source_frames", 0) or 0) - pt, cpath)
    if q0 is None:
        return None
    if q0 < 0 or q0 + k > _file_frames(cpath):
        _LOG.warning("magik.h3: %s's window would run %d frames on, past "
                     "the end of the clip it leads into (%s)",
                     path, k, cpath)
        return None
    return cpath, q0


def _decode_span(path, info, start, n, read_at):
    """`n` frames of the file from `start`, its sound read `read_at`
    audio steps off its frames (see _audio_at_phase)."""
    from . import nodes_encode as ne

    images, audio = ne.decode_window(path, start, n, info=info)
    if abs(read_at) > 1e-6:
        audio = _audio_at_phase(path, start, n, read_at,
                                int(info["frames"]))
    return images, audio


def _join_frames(parts, own):
    """Frames of several files one after the other, each brought to the
    size of parts[own] -- the take's (a parent or child made at another
    size)."""
    import torch

    size = tuple(parts[own].shape[1:3])
    out = []
    for x in parts:
        if tuple(x.shape[1:3]) != size:
            x = torch.nn.functional.interpolate(
                x.movedim(-1, 1), size=size,
                mode="bilinear", align_corners=False).movedim(1, -1)
        out.append(x)
    return torch.cat(out, dim=0)


def _join_audio(parts, own):
    """Several files' sound one after the other, each part an (audio,
    frames) pair, at the rate and channels of parts[own] -- the take's.
    A silent other file is silence for its frames, so every part's sound
    stays under its own frames; a silent take is no sound at all."""
    import torch

    base = parts[own][0]
    if base is None:
        return None
    b = base["waveform"]
    rb = int(base["sample_rate"])
    out = []
    for i, (aud, frames) in enumerate(parts):
        if i == own:
            out.append(b)
            continue
        if aud is None:
            k = int(round(frames / float(fr.FPS) * rb))
            out.append(b.new_zeros(b.shape[:-1] + (k,)))
            continue
        a = aud["waveform"]
        ra = int(aud["sample_rate"])
        if ra != rb:
            import torchaudio
            a = torchaudio.functional.resample(a, ra, rb)
        if a.shape[1] != b.shape[1]:
            a = a.mean(dim=1, keepdim=True).expand(-1, b.shape[1], -1)
        out.append(a.to(b))
    return {"waveform": torch.cat(out, dim=-1), "sample_rate": rb}


def _prepare_pixel_pin(i, spec, snap_down, ctx):
    """A pin from a clip with no usable sidecar: decode, encode, slice.

    The window is chosen in PIXEL space and encoded as a clip of exactly
    that length, which is why this route has no 17-frame cut-grid rule.
    The grid exists because a latent slice must start at cycle phase 0;
    here the encode DEFINES phase 0 at the window's first frame, so any
    cut frame is legal. That is the one thing the pixel route does better
    than the latent one, and it is what makes an arbitrary timeline cut
    continuable at all.

    Everything after the encode goes through _prepare_clip_pin, so the
    slicing rules, the audio boundary arithmetic and the resolved-spec
    bookkeeping have exactly one implementation.
    """
    from . import nodes_encode as ne
    from . import nodes_load

    clip = spec.get("source_path") or ""
    if not clip:
        raise ValueError(
            "MAGIK_H3MCtxApplyPins: spec %d is a pixel source with no "
            "source_path." % i)
    if not ctx or ctx.get("vae") is None or ctx.get("audio_vae") is None:
        raise ValueError(
            "MAGIK_H3MCtxApplyPins: %s has no usable mctx sidecar, so pinning it "
            "means VAE-encoding its pixels -- connect the video_vae "
            "and audio_vae inputs. (Continuity from an encoded source is "
            "pixel-grade, not exact.)" % clip)

    path = nodes_load.resolve_clip_path(clip)
    info = ne.probe_clip(path)
    frames = int(info["frames"])
    take_from = spec.get("take_from", "tail")
    cut = int(spec.get("take_from_frame", 0) or 0)
    if take_from not in ("tail", "head", "at_frame"):
        raise ValueError(
            "MAGIK_H3MCtxApplyPins: spec %d has unknown take_from %r."
            % (i, take_from))

    try:
        header = mctx.read_header(mctx.sidecar_path(path))
    except (OSError, ValueError):
        header = None

    n = int(spec.get("requested_window", 22))
    # The window may run past either end of the file, as the latent route
    # reaches into the take's held frames: a window ending at a cut back
    # into the clip this take continues (_parent_frames), a prepend's on
    # into the clip it leads into (_child_frames). Those frames are read
    # from that clip's file, and only looked for when the file alone is
    # too short.
    reach = ahead = None
    if take_from == "head":
        available = frames
        if n > available and header:
            ahead = _child_frames(path, header, n - available)
        room = available + (n - available if ahead else 0)
    else:
        available = frames
        if take_from == "at_frame":
            if cut > frames and header and spec.get("place") == "after":
                ahead = _child_frames(path, header, cut - frames)
            if cut < 1 or (cut > frames and not ahead):
                raise ValueError(
                    "MAGIK_H3MCtxApplyPins: spec %d's cut frame %d is outside %s "
                    "(1..%d frames at 24 fps)." % (i, cut, clip, frames))
            available = cut
        if n > available and header:
            reach = _parent_frames(path, header, n - available)
        room = available + (n - available if reach else 0)
    if n > room:
        snapped = fr.snap_window(room)
        if not snap_down or snapped is None or snapped == 1:
            raise ValueError(
                "MAGIK_H3MCtxApplyPins: spec %d wants a %d frame window but %s "
                "only offers %d frames (%s). Enable "
                "snap_window_down_to_available to use %s instead."
                % (i, n, clip, room, take_from,
                   snapped if snapped and snapped > 1 else "nothing"))
        _LOG.warning("magik.h3: spec %d window %d -> %d (only %d frames "
                     "available in %s)", i, n, snapped, room, clip)
        n = snapped
    if n == 1:
        raise ValueError(
            "MAGIK_H3MCtxApplyPins: the 1-frame window is not sliceable from a "
            "latent. Use 5 or more; 1-frame pins arrive with the keyframe "
            "path.")

    if take_from == "head":
        start = 0
    else:                       # tail, and at_frame (which ENDS at `cut`)
        start = available - n
    if start < 0 and not reach:
        raise ValueError(
            "MAGIK_H3MCtxApplyPins: spec %d's %d frame window does not fit "
            "before frame %d of %s." % (i, n, available, clip))

    # A clip WITH a sidecar pinned from its pixels: the same window pinned
    # from the sidecar's latents is sliced from the nearest audio step and
    # carries the clip's own phase. Read the sound off the file at that
    # same phase, so both routes hold the same timing. A file with no
    # sidecar has no audio grid: read on frame.
    phase, read_at = 0.0, 0.0
    if header:
        raw_lo = int(header.get("pinned_head_frames", 0) or 0) + start
        src_phase = header_audio_phase(header)
        phase = (src_phase + slice_step(raw_lo, src_phase)
                 - raw_lo * fr.FRAME_RESCALE)
        # an older file's sound already carries the clip's own phase
        read_at = phase if header_audio_synced(header) else (
            phase - header_audio_phase(header))
    own = max(0, start)
    past = max(0, start + n - frames)       # frames after the file's last
    if own >= frames or n - (own - start) - past < 1:
        raise ValueError(
            "MAGIK_H3MCtxApplyPins: spec %d's %d frame window (frames %d..%d) "
            "holds none of %s's own %d frames."
            % (i, n, start, start + n - 1, clip, frames))
    images, audio = _decode_span(path, info, own, n - (own - start) - past,
                                 read_at)
    # The same moments in both files: the held head IS the parent's
    # frames and the held tail the child's, so their sound is read at the
    # take's phase too
    img_parts, aud_parts, mine = [images], [(audio, 0)], 0
    if start < 0:
        ppath, p0 = reach
        pinfo = ne.probe_clip(ppath)
        head_img, head_aud = _decode_span(ppath, pinfo, p0 + start, -start,
                                          read_at)
        img_parts.insert(0, head_img)
        aud_parts.insert(0, (head_aud, -start))
        mine = 1
        _LOG.info("magik.h3: pixel pin from %s reaches %d frames back into "
                  "%s (its frames %d..%d)", clip, -start, ppath,
                  p0 + start, p0 - 1)
    if past:
        cpath, q0 = ahead
        cinfo = ne.probe_clip(cpath)
        tail_img, tail_aud = _decode_span(cpath, cinfo, q0, past, read_at)
        img_parts.append(tail_img)
        aud_parts.append((tail_aud, past))
        _LOG.info("magik.h3: pixel pin from %s runs %d frames on into %s "
                  "(its frames %d..%d)", clip, past, cpath, q0,
                  q0 + past - 1)
    if len(img_parts) > 1:
        images = _join_frames(img_parts, mine)
        audio = _join_audio(aud_parts, mine)
    a_window = int(spec.get("audio_window", 0) or 0)
    if a_window > n:
        _LOG.warning("magik.h3: audio_window %d exceeds the %d frame "
                     "window encoded from %s; the pixel route pins audio "
                     "only over the window it decodes", a_window, n, clip)
    bundle = ne.encode_bundle(
        images, ctx["vae"], ctx["audio_vae"],
        width=ctx["width"], height=ctx["height"],
        fps=fr.FPS,                 # decode_window already resampled
        keep="head", max_frames=0, fit=ctx.get("fit", "cover"),
        audio=audio, audio_phase=phase)
    _LOG.info("magik.h3: pixel pin from %s: frames %d..%d of %d (%s), "
              "encoded at %dx%d -- pixel-grade, unverified",
              clip, start, start + n - 1, int(info["frames"]), take_from,
              ctx["width"], ctx["height"])

    # the encoded bundle IS the window, so the slice is its whole self
    inner = dict(spec, source=bundle, source_kind="clip",
                 take_from="tail", take_from_frame=0,
                 requested_window=n)
    pin = _prepare_clip_pin(i, inner, snap_down)
    # ...but the RECIPE must name the real source and the real range in
    # the original clip's frames, not 0..n of a bundle that exists only
    # inside this call.
    #
    # The hash is the SOURCE FILE's, and it is honest here in a way it
    # would not be on MAGIK_H3MCtxFromFrames: that node takes frames off a
    # WIRE, which may have graded or cropped them since the load, so a
    # hash there would describe pixels that no longer exist. This route
    # opened the file itself, so "encoded from frames %d..%d of the file
    # with this hash" is simply true -- and without it the take is a
    # ROOT, which leaves the Result Preview with no pair to show, the
    # seam with no link to repair, and the timeline butt-joining at the
    # parent's full length instead of at the cut.
    source_id = nodes_load._cached_hash(path)
    pin["spec"] = dict(
        {k: v for k, v in spec.items() if k != "source"},
        source_kind="clip_pixels", source_id=source_id,
        source_start=start, source_frames=n)
    pin["source_id"] = source_id
    return pin


# ---------------------------------------------------------------------------
# audio phase: where a clip's stored sound sits against its own frames
# ---------------------------------------------------------------------------
#
# Audio steps (40 Hz) and frames (24 fps) meet only every third frame. A
# pin window starting between two steps is sliced from the nearest one,
# so the take's sound -- held and generated alike -- runs a third of a
# step early or late against its own picture. That is NOT corrected in
# the latents: a take's sound stays on its parent's audio grid, which is
# exactly what the joint upscale lays every clip on (one grid per
# stretch), and what makes a chain of extends line up there. It is
# corrected where sound meets frames: the delivered MP4's sound is cut
# that much earlier or later (MAGIK_H3TrimPinned), so the file, and every
# preview and export built from files, plays in sync.
#
# The phase is in audio steps: + means the sound at a given moment of
# the file is from slightly LATER than the picture (cut it earlier).


def shifted_samples(wave, lo, hi):
    """wave[..., lo:hi], mirroring the sound past either end instead of
    leaving silence (a shift of a few ms can reach just past the decoded
    sound, at the very place a join is made)."""
    import torch

    n = int(wave.shape[-1])
    a, b = max(0, lo), min(n, hi)
    if a >= b:
        return wave[..., :0].new_zeros(wave.shape[:-1] + (max(0, hi - lo),))
    out = wave[..., a:b]
    if lo < a:
        k = a - lo
        head = wave[..., a + 1:a + 1 + k].flip(-1)
        if int(head.shape[-1]) < k:
            head = torch.cat([head.new_zeros(head.shape[:-1] + (
                k - int(head.shape[-1]),)), head], dim=-1)
        out = torch.cat([head, out], dim=-1)
    if hi > b:
        k = hi - b
        tail = wave[..., max(0, b - 1 - k):b - 1].flip(-1)
        if int(tail.shape[-1]) < k:
            tail = torch.cat([tail, tail.new_zeros(tail.shape[:-1] + (
                k - int(tail.shape[-1]),))], dim=-1)
        out = torch.cat([out, tail], dim=-1)
    return out


def slice_step(frame, phase):
    """The audio step a window starting at `frame` is sliced from: the one
    nearest that frame's moment in TRUE time, for a source whose sound
    sits `phase` steps off its frames. Choosing it this way (not merely
    the step nearest the frame on the source's own grid) keeps every
    take within half a step of its frames: phases never add up along a
    chain."""
    return int(math.floor(frame * fr.FRAME_RESCALE - float(phase) + 0.5))


def phase_samples(phase, rate):
    """A phase in audio steps as whole samples at `rate`."""
    return int(round(float(phase) * rate / float(fr.AUDIO_LATENT_FPS)))


def pins_audio_phase(pins, raw_frames):
    """The take's audio phase, from the pins that made it (resolved pins,
    as Apply returns them). The extend side decides on a bridge: its held
    window is where the take's sound starts from."""
    pins = list(pins or [])
    for p in pins:
        if p.get("place") == "before":
            return float(p.get("audio_phase", 0.0) or 0.0)
    for p in pins:
        if p.get("place") == "after" and p.get("audio_steps"):
            # held at the take's END: its first step sits where the
            # masked placement puts it, which may itself be between steps
            covered = int(p.get("covered", 0) or 0)
            a_lo = fr.audio_total(int(raw_frames)) - int(p["audio_steps"])
            gap = a_lo - (int(raw_frames) - covered) * fr.FRAME_RESCALE
            return float(p.get("audio_phase", 0.0) or 0.0) - gap
    return 0.0


def header_audio_phase(header):
    """A saved clip's audio phase. Recorded by takes saved since it was
    (`audio_phase`); for older ones, worked out from the recipe the way
    their slice was made, taking the clip they continue as on its own
    grid (a root, or a take whose length is on the shared AV grid)."""
    if not header:
        return 0.0
    v = header.get("audio_phase")
    if v not in (None, ""):
        try:
            return float(v)
        except (TypeError, ValueError):
            pass
    pins = mctx.parse_pins(header)
    raw = int(header.get("raw_frames", 0) or 0)

    def sliced(p):
        if p.get("source_kind") != "clip":
            return None              # encoded from pixels: on its frame
        start = int(p.get("source_start", 0) or 0)
        n = int(p.get("source_frames", 0) or 0)
        a_lo = start + n - (int(p.get("audio_window", 0) or 0) or n)
        return fr.audio_total(a_lo) - a_lo * fr.FRAME_RESCALE, n, start, a_lo
    for p in pins:
        if p.get("place") == "before":
            got = sliced(p)
            return float(got[0]) if got else 0.0
    for p in pins:
        if p.get("place") == "after":
            got = sliced(p)
            if not got:
                return 0.0
            off, n, start, a_lo = got
            steps = fr.audio_total(start + n) - fr.audio_total(a_lo)
            gap = (fr.audio_total(raw) - steps) - (raw - n) * fr.FRAME_RESCALE
            return float(off - gap)
    return 0.0


def _audio_at_phase(path, start, n, phase, frames):
    """The file's sound under frames start..start+n, read `phase` audio
    steps later (earlier when negative). Past either end of the file's
    sound (`frames` long: our files are cut to their frames) it is
    mirrored rather than silent -- an extend's window ends at the clip's
    last frame, which is exactly where the take continues from."""
    from . import nodes_encode as ne

    step = phase / float(fr.AUDIO_LATENT_FPS)
    t0 = start / float(fr.FPS) + step
    t1 = (start + n) / float(fr.FPS) + step
    end = frames / float(fr.FPS)
    a, b = max(0.0, t0), min(t1, end)
    got = ne._decode_audio_range(path, a, b)
    if got is None or (t0 >= 0 and t1 <= end):
        return got
    sr = int(got["sample_rate"])
    wave = got["waveform"]
    lead = int(round((a - t0) * sr))
    total = int(round((t1 - t0) * sr))
    return {"waveform": shifted_samples(wave, -lead, total - lead),
            "sample_rate": sr}


def header_audio_synced(header):
    """Was this clip's MP4 saved with its sound cut on its own frames?"""
    return str((header or {}).get("audio_synced", "")) == "1"


def _prepare_clip_pin(i, spec, snap_down):
        bundle = spec.get("source")
        if bundle is None:
            raise ValueError(
                "MAGIK_H3MCtxApplyPins: spec %d carries no source bundle. Specs must "
                "come from MAGIK_H3MCtxPinSpec in this same run." % i)
        meta = bundle["meta"]
        video, audio = bundle["video_latent"], bundle["audio_latent"]

        raw_steps = int(video.shape[2])
        raw_frames = fr.pixel_frames(raw_steps)
        meta_raw = int(meta.get("raw_frames", raw_frames))
        if meta_raw != raw_frames:
            raise ValueError(
                "MAGIK_H3MCtxApplyPins: sidecar header says %d raw frames but the "
                "stored latent covers %d. The sidecar is inconsistent; "
                "refusing." % (meta_raw, raw_frames))
        delivered = int(meta.get("delivered_frames", raw_frames))
        pinned_head = int(meta.get("pinned_head_frames", 0))
        pinned_tail = int(meta.get("pinned_tail_frames", 0) or 0)
        take_from = spec.get("take_from", "tail")
        cut_frame = int(spec.get("take_from_frame", 0))

        # Window availability depends on where it's taken from -- and the
        # room is the source's whole stored latent: a window ending at a
        # cut may reach back into the frames the source held from the
        # clip it continues (its pinned head), and a prepend's window may
        # reach on into the ones it led into (its pinned tail). They are
        # the source's own latents, continuous with what it delivered, so
        # a very short take can still be continued; the window's END,
        # where the new take joins, stays where it was asked to be.
        if take_from == "at_frame":
            hi = delivered + (pinned_tail
                              if spec.get("place") == "after" else 0)
            if cut_frame < 1 or cut_frame > hi:
                raise ValueError(
                    "MAGIK_H3MCtxApplyPins: spec %d's cut frame %d is outside the "
                    "source's delivered range (1..%d)."
                    % (i, cut_frame, hi))
            available = pinned_head + cut_frame
        elif take_from == "tail":
            available = pinned_head + delivered
        elif take_from == "head":
            available = delivered + pinned_tail
        else:
            raise ValueError(
                "MAGIK_H3MCtxApplyPins: spec %d has unknown take_from %r."
                % (i, take_from))

        n = int(spec.get("requested_window", 22))
        if n > available:
            snapped = fr.snap_window(available)
            if not snap_down or snapped is None:
                raise ValueError(
                    "MAGIK_H3MCtxApplyPins: spec %d wants a %d frame window but "
                    "only %d frames are available (%s). Enable "
                    "snap_window_down_to_available to use %s instead."
                    % (i, n, available, take_from,
                       snapped if snapped else "nothing"))
            _LOG.warning("magik.h3: spec %d window %d -> %d (only %d frames "
                         "available)", i, n, snapped, available)
            n = snapped
        if n == 1:
            raise ValueError(
                "MAGIK_H3MCtxApplyPins: the 1-frame window is not sliceable from a "
                "latent (the last step of a clip spans 4 frames). Use 5 or "
                "more; 1-frame pins arrive with the keyframe path.")

        steps = fr.steps_for_frames(n)
        if steps is None:
            raise RuntimeError(
                "MAGIK_H3MCtxApplyPins: %d frames is not a whole number of latent "
                "steps; the window ladder no longer matches the VAE grid."
                % n)

        # Delivered-frame start of the slice, then mapped onto the RAW
        # timeline (the stored latent includes any pinned scaffolding).
        if take_from == "tail":
            d_start = delivered - n
        elif take_from == "head":
            d_start = 0
        else:
            d_start = cut_frame - n
        raw_start = pinned_head + d_start
        if d_start < 0:
            _LOG.info("magik.h3: spec %d's %d frame window reaches %d frames "
                      "back into the clip its source continues (the "
                      "source's held head)", i, n, -d_start)

        # Latent-grade cut rule: phase-0 slice starts occur only at
        # 17-frame group boundaries. This one check covers everything the
        # old special cases did -- pinned tails, misaligned continuation
        # heads (DESIGN 4a), arbitrary interior cuts.
        if raw_start % fr.FRAMES_PER_GROUP != 0:
            lo = (raw_start // fr.FRAMES_PER_GROUP) * fr.FRAMES_PER_GROUP
            hi = lo + fr.FRAMES_PER_GROUP
            suggest = sorted(set(
                b - pinned_head + n for b in (lo, hi)
                if 0 <= b and 1 <= b - pinned_head + n <= delivered))
            raise ValueError(
                "MAGIK_H3MCtxApplyPins: spec %d's window would start at raw frame "
                "%d, which is not on the 17-frame latent grid -- the slice "
                "would be unsound. Nearest latent-grade window END frames: "
                "%s. (Or cross to pixels via H3 MCtx From Frames, which "
                "encodes any cut at the cost of exactness.)"
                % (i, raw_start, suggest))
        k = raw_start // fr.FRAMES_PER_GROUP * fr.LATENTS_PER_GROUP
        if fr.frame_at_latent(k) != raw_start:
            raise RuntimeError(
                "MAGIK_H3MCtxApplyPins: step mapping disagreement (frame %d -> "
                "step %d -> frame %d). Upstream grid change; refusing."
                % (raw_start, k, fr.frame_at_latent(k)))
        raw_end = raw_start + n
        if raw_end > raw_frames or k + steps > raw_steps:
            raise ValueError(
                "MAGIK_H3MCtxApplyPins: spec %d's window (raw frames %d..%d) "
                "exceeds the stored latent (%d frames)."
                % (i, raw_start, raw_end, raw_frames))

        video_slice = video[:1, :, k:k + steps].clone()
        covered = fr.pixel_frames(steps)
        if covered != n:
            raise RuntimeError(
                "MAGIK_H3MCtxApplyPins: %d steps cover %d frames, expected %d. "
                "Upstream VAE grid change; refusing." % (steps, covered, n))

        # Audio: end-aligned with the video window's END. It starts on the
        # step nearest the window's first frame in TRUE time -- the
        # source's own audio phase included (slice_step) -- so the take's
        # sound is never more than half a step off its frames, however
        # many off-grid windows a chain goes through.
        a_frames = int(spec.get("audio_window", 0)) or n
        total_t = int(audio.shape[-1])
        a_lo = max(0, raw_end - a_frames)
        meta_phase = header_audio_phase(meta)
        start_idx = max(0, slice_step(a_lo, meta_phase))
        want = fr.audio_span(a_lo, raw_end)
        end_idx = min(total_t, start_idx + want)
        rt = max(0, end_idx - start_idx)
        if rt < want:
            _LOG.warning("magik.h3: audio window wants %d steps, the latent "
                         "supplies %d; pinning what there is", want, rt)
        if raw_end == raw_frames and fr.audio_overhang(total_t,
                                                       raw_frames) is None:
            _LOG.warning("magik.h3: unexpected audio grid (%d steps for %d "
                         "frames)", total_t, raw_frames)
        audio_slice = (audio[:1, ..., start_idx:end_idx].clone()
                       if rt > 0 else None)
        # Where the held sound sits against the window's first frame, in
        # audio steps (see pins_audio_phase), and so how far its end
        # reaches past the window's last frame
        phase = meta_phase + (start_idx - a_lo * fr.FRAME_RESCALE)
        overhang = phase + rt - (raw_end - a_lo) * fr.FRAME_RESCALE

        return {
            "kind": "clip",
            "place": spec.get("place", "before"),
            "video": video_slice,
            "steps": steps,
            "covered": covered,
            "audio": audio_slice,
            "audio_steps": rt,
            "overhang": overhang,
            "audio_phase": phase,
            "width": int(video.shape[4]) * 16,
            "height": int(video.shape[3]) * 16,
            "origin": bundle.get("origin", "sampled"),
            "source_id": bundle.get("self_id", ""),
            # the AUTHORITATIVE resolved spec: source range recomputed from
            # the actually-sliced window (snap_down may have shrunk it), so
            # downstream trim/save read what really happened. source_start
            # is in the source's RAW coordinates -- that is what the
            # sidecar recipe means (summarize_pins derives parent_join_
            # frame from it, seam_report slices the raw latent with it);
            # the delivered-based d_start only equals it for root sources.
            "spec": dict(
                {k: v for k, v in spec.items() if k != "source"},
                source_start=raw_start,
                source_frames=n,
                # the source's audio steps it holds: where the upscale lays
                # this clip's sound against its parent's (nodes_joint)
                audio_start_step=start_idx,
                audio_steps=rt,
            ),
        }


def _split_modes(pins):
    """(guide_pins, masked_pins) of one stage's pins.

    "both" is in each list: the SAME pin, so it is still one pin per
    side, trimmed once (pins_trim_totals reads `pins`, not these), and
    the keyframe rows and the mask describe the same window from the
    same slice.
    """
    guide, masked = [], []
    for pin in pins:
        mode = (pin.get("spec") or {}).get("mode", "guide")
        if mode in ("masked", "both"):
            masked.append(pin)
        if mode in ("guide", "both"):
            guide.append(pin)
        if mode == "guide" and any(
                (pin.get("spec") or {}).get(k)
                for k in ("mask_ramp_frames", "mask_ramp_edge",
                          "mask_hold")):
            _LOG.warning(
                "magik.h3: pin %s carries a mask shape but rides in "
                "guide mode, which has no mask -- the shape is "
                "ignored. Switch the mode to masked or both.",
                pin.get("place"))
    return guide, masked


def _stage_keyframes(pins, frame_count, width, height, stage_label=""):
    """Native keyframe rows for one stage's guide pins.

    Pinned run coordinates on that stage's target timeline. before:
    indices 0..covered-1 (extend; trimmed off the head). after: the LAST
    covered indices (prepend; generation runs TOWARD the pinned
    content, which generalizes stock's trained-in last-frame anchor;
    trimmed off the tail). BOTH AT ONCE = bridging: the run departs
    the before-context and must arrive at the after-context; the
    free frames in between are the generated connective content.
    One keyframe entry carries each whole window: core assigns its
    steps cond_t + cumsum(FRAME_RESCALE * FRAME_PER_TOKEN[k % 5])
    from k=0, which matches the target's own step pattern at `base`
    because both the slice start and `base` sit on the 17-frame
    group grid (phase 0; _prepare_pins refused anything else).
    """
    keyframes = []
    for pin in pins:
        origin = _keyframe_origin(pin)
        place = pin.get("place")
        covered = int(pin["covered"])
        base = 0 if place == "before" else frame_count - covered
        keyframes.append({
            "resolved_frame_index": base,
            "latent": pin["video"],
        })
        if origin:
            keyframes[-1]["origin"] = origin

        rt = int(pin.get("audio_steps", 0))
        pin_audio = pin.get("audio") if rt > 0 else None
        end_frame = 0.0
        if pin_audio is not None:
            # End-align the pinned audio with the pinned video's END
            # on the target timeline: frame `covered` for a
            # before-pin, `frame_count` for an after-pin. The sliced
            # audio reaches `overhang` of a step past the source
            # slice's end, or short of it when negative (nonzero only
            # when the slice ends at the source clip's end), so the
            # end coordinate moves by that much, then snaps onto the
            # target's own audio grid (a
            # third of a step is 8.3 ms; the snap stops the offset
            # cycling across chains). Core anchors an audio window's
            # START at FRAME_RESCALE * resolved_frame_index and
            # advances 1.0 per audio latent step, so ending at
            # integer audio coordinate `end_coord` means a fractional
            # frame index -- fine, PackedLayout only ever multiplies
            # it (the int restriction lives in the AddGuide node, not
            # the model).
            end_base = covered if place == "before" else frame_count
            end_frame = (float(end_base) +
                         float(pin["overhang"]) / fr.FRAME_RESCALE)
            end_coord = round(fr.FRAME_RESCALE * end_frame)
            end_frame = end_coord / fr.FRAME_RESCALE
            keyframes.append({
                "resolved_frame_index":
                    (end_coord - rt) / fr.FRAME_RESCALE,
                "audio_latent": pin_audio,
            })
            if origin:
                keyframes[-1]["origin"] = origin

        _LOG.info(
            "magik.h3: %spinned %d frames (%d steps) at the %s of a %d "
            "frame clip at %dx%d (indices %d..%d); audio %s; trim %d "
            "off the %s",
            stage_label, covered, int(pin["steps"]),
            "head" if place == "before" else "tail",
            frame_count, width, height, base, base + covered - 1,
            ("%d steps ending at frame %.3f" % (rt, end_frame))
            if pin_audio is not None else "off", covered,
            "head" if place == "before" else "tail")
    return keyframes


def _dual_lr_pin(i, hr_pin, orig_spec, lr_w, lr_h, video_vae, audio_vae):
    """The stage-1 twin of one resolved high-resolution pin.

    The record is authoritative (rule R3): the encode is driven by the
    resolved window -- its length, with snapping OFF -- through the
    unchanged _prepare_pixel_pin (Path C, rule R2), so every window,
    file and reach rule is the one implementation. It is then verified
    to cover the SAME file interval and, for a sidecar source, to BE
    the same file (the hash must still pair, or the two stages would
    pin different content). The audio slice is the HR record's own,
    shared (rule R4): audio has no spatial axes, so one slice and one
    set of timing numbers serve both stages.
    """
    s = hr_pin["spec"]
    n = int(s["source_frames"])
    inner = dict(s)
    inner["source_kind"] = "clip_pixels"     # force the pixel route
    inner["requested_window"] = n            # the window is already resolved
    inner["audio_window"] = 0                # audio is shared, not re-read (R4)
    lr_pin = _prepare_pixel_pin(i, inner, False,
                                {"vae": video_vae, "audio_vae": audio_vae,
                                 "width": lr_w, "height": lr_h})
    if int(lr_pin["covered"]) != n:
        raise RuntimeError(
            "MAGIK_H3MCtxApplyPins: the stage-1 encode of spec %d covered "
            "%d frames, not the resolved window's %d; refusing rather "
            "than pinning two different intervals."
            % (i, int(lr_pin["covered"]), n))
    # Interval identity, in the source file's own frames: the record's
    # window end (raw minus the held head, which the file does not
    # carry) must be the window the pixel route decoded.
    head = 0
    if s.get("source_kind") == "clip":
        meta = (orig_spec.get("source") or {}).get("meta") or {}
        head = int(meta.get("pinned_head_frames", 0) or 0)
        if lr_pin.get("source_id") != hr_pin.get("source_id"):
            raise ValueError(
                "MAGIK_H3MCtxApplyPins: the file behind the stage-1 encode "
                "of spec %d hashes to %s..., but the sidecar that verified "
                "the high-resolution slice says %s... -- the file has "
                "changed since the clip was saved. Re-save the clip, or "
                "pin from its current file."
                % (i, str(lr_pin.get("source_id"))[:12],
                   str(hr_pin.get("source_id"))[:12]))
    hr_end = int(s["source_start"]) + n - head
    lr_end = (int(lr_pin["spec"]["source_start"])
              + int(lr_pin["spec"]["source_frames"]))
    if lr_end != hr_end:
        raise ValueError(
            "MAGIK_H3MCtxApplyPins: spec %d's two stages pin different "
            "moments -- the resolved window ends at file frame %d, the "
            "stage-1 encode at %d. (The file's frames may not match its "
            "sidecar; re-save the clip.)" % (i, hr_end, lr_end))
    # R4: one audio slice, computed once by the exact route, shared.
    for k in ("audio", "audio_steps", "overhang", "audio_phase"):
        lr_pin[k] = hr_pin.get(k)
    for k in ("audio_start_step", "audio_steps"):
        if k in s:
            lr_pin["spec"][k] = s[k]
        else:
            lr_pin["spec"].pop(k, None)
    return lr_pin


class MAGIK_H3MCtxApplyPins:
    CATEGORY = "magik/h3"
    FUNCTION = "apply"
    RETURN_TYPES = ("CONDITIONING", "LATENT", wt.PINS)
    RETURN_NAMES = ("conditioning", "latent", "pins")
    DESCRIPTION = (
        "Slices the pinned windows out of the sources (the sole "
        "frame/latent math site: window snapping, phase checks, "
        "cumulative-total audio cut) and attaches each pinned run to the "
        "conditioning as native H3 keyframe anchors. The pins "
        "output carries the resolved slices -- feed it to Trim/Save so "
        "they read what was actually pinned. Turn on two_stage for a "
        "low-res -> upscale -> high-res flow: this node then pins stage 1 "
        "(pixel-grade, at this latent's size) and H3 MCtx Apply Pins "
        "Stage 2 pins the exact full-resolution slices on the upscaled "
        "latent."
    )
    OUTPUT_TOOLTIPS = (
        "Conditioning with the pinned runs attached. Feed the guider/sampler.",
        "The target latent. ALWAYS wire the sampler from here, not from "
        "the empty-latent node: masked pins write their preserved windows "
        "and denoise mask into it (guide-only runs pass it through "
        "untouched).",
        "The resolved pins. Feed MAGIK_H3TrimPinned / the Save nodes (in "
        "two-stage mode, wire the pins to Apply Pins Stage 2 and take "
        "Trim/Save's pins from there).",
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "conditioning": ("CONDITIONING",),
                "latent": ("LATENT", {
                    "tooltip": "The TARGET clip's empty AV latent -- "
                               "authoritative for frame count and "
                               "resolution. Wire the same latent to the "
                               "sampler."}),
                "snap_window_down_to_available": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "When a source cannot supply the requested "
                               "window, snap down the ladder (56/39/22/5) "
                               "instead of refusing. Off = refuse loudly; "
                               "snapping silently weakens continuity."}),
                "freeze_audio": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "Re-render PICTURE ONLY: hold the whole "
                               "audio mask at 0 so the soundtrack comes "
                               "out exactly as it went in. For an upscale/"
                               "refine pass, where the sound is already "
                               "finished -- the sampler denoises the "
                               "nested AV pair together, so without this "
                               "a refine re-renders audio nobody asked it "
                               "to touch. Works with no pins at all, "
                               "which is what the first clip of a "
                               "timeline needs."}),
            },
            "optional": {
                "video_vae": ("VAE", {
                    "tooltip": "Video VAE. Needed ONLY for pins from clips "
                               "with no usable mctx sidecar, which have to "
                               "be encoded from their pixels (the Timeline "
                               "emits those for imported footage). Pins "
                               "from a sidecar never touch a VAE."}),
                "audio_vae": ("VAE", {
                    "tooltip": "Audio VAE, for the same pixel-encoded pins "
                               "as video_vae -- required alongside it even "
                               "when the footage is silent."}),
                "pin_specs": (wt.PINSPECS, {
                    "tooltip": "The spec stack from MAGIK_H3MCtxPinSpec or a "
                               "loader's create_pins output. Unconnected or "
                               "empty = pass-through: the conditioning is "
                               "returned untouched and pins is empty (a "
                               "plain root generation), so the node can "
                               "stay in the graph when not pinning."}),
                "masked_audio_feather": ("INT", {
                    "default": 8, "min": 0, "max": 256,
                    "tooltip": "Masked pins only: half-cosine release of "
                               "the preserved audio across its final ticks "
                               "toward the generated region (8 = 0.2 s). "
                               "0 = hard mask edge."}),
                "audio_denoise": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 1.0, "step": 0.05,
                    "tooltip": "With freeze_audio: hold the audio mask at "
                               "this value instead of 0, so the sound "
                               "re-samples alongside the picture. A "
                               "refine at high sigma needs ~0.5 here for "
                               "the model to re-derive LIP SYNC (frozen "
                               "audio gives a near-noise video nothing "
                               "to move the mouth for). The resampled "
                               "audio must then be DISCARDED at save "
                               "time: wire the save node's audio from a "
                               "decode of the SOURCE audio latent. "
                               "Junction-pinned windows stay frozen "
                               "regardless. 0 = today's exact freeze."}),
                "two_stage": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "Two-stage (low-res first sampler, upscale, "
                               "high-res second sampler). ON: this node "
                               "pins the window at THIS latent's "
                               "resolution (pixel-grade re-encode of the "
                               "source file) and sends the exact "
                               "full-resolution slices on `pins` to H3 "
                               "MCtx Apply Pins Stage 2, which pins them "
                               "on the upscaled latent. Needs both VAEs "
                               "and a sidecar source. OFF: ordinary "
                               "single-resolution pinning."}),
            },
            "hidden": {
                "dynprompt": "DYNPROMPT",
                "unique_id": "UNIQUE_ID",
            },
        }

    @staticmethod
    def _all_input_values(dynprompt):
        for node_id in dynprompt.all_node_ids():
            try:
                node = dynprompt.get_node(node_id)
            except Exception:
                continue
            yield from (node.get("inputs") or {}).values()

    def apply(self, **kwargs):
        out = self._apply(**kwargs)
        # the live preview (nodes_live) trims nothing but has to know where
        # the take sits inside the latent it watches: the pins prepared for
        # a latent of this length
        try:
            from .nodes_live import note_pins
            video, _ = unpack_av(out[1], name="latent")
            note_pins(int(video.shape[2]), out[2])
        except Exception as exc:
            _LOG.debug("magik.h3: pins not noted for the live preview (%s)",
                       exc)
        return out

    def _apply(self, conditioning, latent, snap_window_down_to_available,
               freeze_audio=False, pin_specs=None, video_vae=None,
               audio_vae=None, masked_audio_feather=8, audio_denoise=0.0,
               dynprompt=None, unique_id=None, two_stage=False):
        if two_stage:
            return self._apply_stage1(
                conditioning, latent, snap_window_down_to_available,
                freeze_audio, pin_specs, video_vae, audio_vae,
                masked_audio_feather, audio_denoise, dynprompt, unique_id)
        if not pin_specs:
            if freeze_audio:
                # the first clip of a refine has nothing to pin and still
                # must not have its sound regenerated, so this is a real
                # case rather than a degenerate one
                _LOG.info("magik.h3: no pin specs, but audio is frozen; "
                          "the conditioning passes through and the latent "
                          "carries an audio-only mask")
                return (conditioning,
                        nodes_masked.apply_masked_pins(
                            latent, [], masked_audio_feather,
                            freeze_audio=True,
                            audio_denoise=audio_denoise),
                        [])
            _LOG.info("magik.h3: no pin specs; conditioning passes through "
                      "untouched (plain root generation)")
            return (conditioning, latent, [])
        for i, s in enumerate(pin_specs or []):
            if s.get("place") == "at_frame":
                raise ValueError(
                    "MAGIK_H3MCtxApplyPins: spec %d places 'at_frame' -- not yet "
                    "implemented; arrives with inside pins/repaint (a later "
                    "phase)." % i)

        # The target latent is read BEFORE slicing now: a pixel source is
        # encoded to this resolution rather than merely checked against
        # it, so the answer has to be in hand first.
        target_video, _ = unpack_av(latent, name="latent")
        latent_t = int(target_video.shape[2])
        frame_count = fr.pixel_frames(latent_t)
        width = int(target_video.shape[4]) * 16
        height = int(target_video.shape[3]) * 16

        pins = _prepare_pins(
            pin_specs, snap_window_down_to_available,
            ctx={"vae": video_vae, "audio_vae": audio_vae,
                 "width": width, "height": height})
        n_before = sum(1 for p in pins if p.get("place") == "before")
        n_after = sum(1 for p in pins if p.get("place") == "after")
        if n_before > 1 or n_after > 1:
            raise ValueError(
                "MAGIK_H3MCtxApplyPins: at most one pin per side; got %d before "
                "/ %d after. (Several pins on the same side have no "
                "coordinate meaning.)" % (n_before, n_after))

        for pin in pins:
            if (pin["width"], pin["height"]) != (width, height):
                raise ValueError(
                    "MAGIK_H3MCtxApplyPins: the pinned clip is %dx%d but this "
                    "clip is %dx%d. A latent cannot be resized; regenerate "
                    "the source at this resolution, or cross to pixels "
                    "explicitly (H3 MCtx From Frames, which resizes to this "
                    "clip's latent)."
                    % (pin["width"], pin["height"], width, height))
            if pin.get("origin") == "encoded":
                _LOG.info("magik.h3: pin from an encoded (pixel-grade) "
                          "source; continuity is soft, not exact")
        total_covered = sum(int(p["covered"]) for p in pins)
        if total_covered >= frame_count:
            raise ValueError(
                "MAGIK_H3MCtxApplyPins: pinning %d frames into a %d frame clip; "
                "the generated part must have room -- raise the clip "
                "length or shrink the pin window(s)."
                % (total_covered, frame_count))

        # "both" is in each list: the SAME pin, so it is still one pin
        # per side, trimmed once (pins_trim_totals reads `pins`, not
        # these), and the keyframe rows and the mask describe the same
        # window from the same slice. (_split_modes, inside
        # _stage_outputs.)
        bridge = (frame_count - total_covered
                  if (n_before and n_after) else None)
        out, latent_out = self._stage_outputs(
            "", conditioning, latent, pins, frame_count, width, height,
            masked_audio_feather, freeze_audio, audio_denoise,
            dynprompt, unique_id, 1, bridge)
        return (out, latent_out, pins)

    @staticmethod
    def _output_wired(dynprompt, unique_id, slot):
        """Whether this node's output `slot` feeds anything in this run.

        A masked pin acts through a latent OUTPUT. If nothing consumes
        it, the sampler runs on the untouched empty latent and the
        "extend" is silently a root generation -- a wrong-but-plausible
        default, so refuse instead. Scan the DYNPROMPT, not the PROMPT:
        under the upscale loop's graph expansion this node runs as a
        prefixed clone, and only the dynamic prompt holds the clones
        (and their links) under the ids this unique_id can match. No
        hidden inputs (headless and test runs) counts as wired, the
        leniency the check has always shown.
        """
        if dynprompt is None or unique_id is None:
            return True
        return any(isinstance(v, list) and len(v) == 2
                   and str(v[0]) == str(unique_id) and int(v[1]) == slot
                   for v in MAGIK_H3MCtxApplyPins._all_input_values(dynprompt))

    @staticmethod
    def _strip_inherited_mask(latent):
        """The stage-2 target without any noise mask it inherited.

        A stage-1 latent output feeds the upscaler, and upscalers pass
        the latent dict through -- so the stage-2 target can arrive
        carrying stage 1's mask, at stage 1's dims. Upstream refuses to
        compose masks on a latent that already carries one, and the
        two-stage flow is exactly the one that produces this, so the
        node strips the inherited mask (loudly) rather than teaching
        users to re-wire empty latents.
        """
        if latent.get("noise_mask") is not None:
            _LOG.info(
                "magik.h3: the stage-2 target arrives with a noise mask "
                "(the stage-1 mask rode through the upscaler); stripping "
                "it -- stage 2 writes a fresh mask at its own resolution")
            return {k: v for k, v in latent.items() if k != "noise_mask"}
        return latent

    def _stage_outputs(self, stage_label, conditioning, latent, stage_pins,
                       frame_count, width, height, masked_audio_feather,
                       freeze_audio, audio_denoise, dynprompt, unique_id,
                       latent_slot, bridge_frames=None):
        """One stage's (conditioning, latent) from its own prepared pins.

        The single-resolution path is the stage_label=="" call with the
        target's own geometry; dual mode calls it once per stage, each
        from the PRISTINE conditioning, so the stages' keyframe rows
        and masks are built per stage and never mixed (rule R5).
        """
        guide_pins, masked_pins = _split_modes(stage_pins)
        if masked_pins and not nodes_masked.core_masks_available():
            raise ValueError(
                "MAGIK_H3MCtxApplyPins: masked pins need ComfyUI's native "
                "per-stream H3 denoise masks (PR #15375, merged "
                "2026-08-18). Update ComfyUI, or switch the pin's mode "
                "to guide.")
        if guide_pins:
            _ensure_native_anchors()

        keyframes = _stage_keyframes(guide_pins, frame_count, width,
                                     height, stage_label)
        if bridge_frames is not None:
            _LOG.info(
                "magik.h3: BRIDGING -- %d free frames generated between "
                "the pinned head and tail; the run must depart one "
                "context and arrive at the other (seam convergence on "
                "both ends is seed-dependent, like prepend)",
                bridge_frames)

        # Append to any keyframes already on the conditioning (a stock
        # first/last frame, an AddGuide chain): coordinates now come from
        # one native rulebook, so e.g. extend-toward-image = this pin +
        # a stock last-frame keyframe is a legal combination.
        out = conditioning
        if keyframes:
            prior = list(conditioning[0][1].get("minimax_keyframes", []))
            if prior:
                _LOG.info("magik.h3: appending pin to %d existing keyframe "
                          "anchor(s) on the conditioning", len(prior))
            out = node_helpers.conditioning_set_values(conditioning, {
                "minimax_keyframes": prior + keyframes,
            })
        latent_out = latent
        if masked_pins:
            if not self._output_wired(dynprompt, unique_id, latent_slot):
                raise ValueError(
                    "MAGIK_H3MCtxApplyPins: this run has masked pins, but the "
                    "node's %s is not wired to anything. "
                    "Masked pins take effect through that latent -- wire "
                    "it to the sampler (in place of the empty latent), "
                    "or the preserved window never reaches sampling."
                    % "latent output")
            latent_out = nodes_masked.apply_masked_pins(
                latent, masked_pins, masked_audio_feather,
                freeze_audio=freeze_audio, audio_denoise=audio_denoise)
        elif freeze_audio:
            # guide-only pins carry no mask of their own, so the freeze
            # would otherwise be accepted and silently do nothing
            latent_out = nodes_masked.apply_masked_pins(
                latent, [], masked_audio_feather, freeze_audio=True,
                audio_denoise=audio_denoise)
        return out, latent_out

    def _apply_stage1(self, conditioning, latent,
                      snap_window_down_to_available, freeze_audio,
                      pin_specs, video_vae, audio_vae,
                      masked_audio_feather, audio_denoise, dynprompt,
                      unique_id):
        """Two-stage mode, stage 1: ONE pin window, two representations.

        This node builds stage 1 only; MAGIK_H3MCtxApplyPinsStage2 builds
        stage 2 from the upscaled, sampled latent. No input of either
        node depends on its own outputs.

        R1  The stage-2 (full-resolution) pin is the exact sidecar
            slice, through the unchanged _prepare_clip_pin path; it
            rides the pins wire at the source's own resolution.
        R2  The stage-1 pin is pixel-grade by construction: the SAME
            window re-encoded from the source FILE at this latent's
            resolution (Path C), through the unchanged
            _prepare_pixel_pin.
        R3  One shared temporal record, resolved once. The stage-1
            encode is DRIVEN by the resolved record (its window length,
            snap off) and verified to cover the same file interval.
        R4  The audio slice is computed once by the exact route and
            shared; audio timing is metadata on the record.
        R5  Conditioning is per stage, built from the pristine input;
            stage 2 strips an inherited mask before writing its own.
        R6  The log names the pin, the source interval, both
            resolutions, the grade of each representation and the
            actual (possibly snapped) window.
        R7  The sidecar format does not change: the pins wire carries
            the exact record (source_kind 'clip', lineage grade
            'latent') with per-stage facts as run-time fields that
            serialize_pins never copies.
        """
        if not pin_specs:
            _LOG.info(
                "magik.h3: no pin specs in two-stage mode; audio is %s",
                "frozen" if freeze_audio else "untouched")
            lat1 = latent
            if freeze_audio:
                lat1 = nodes_masked.apply_masked_pins(
                    latent, [], masked_audio_feather, freeze_audio=True,
                    audio_denoise=audio_denoise)
            return (conditioning, lat1, [])
        for i, s in enumerate(pin_specs):
            if s.get("place") == "at_frame":
                raise ValueError(
                    "MAGIK_H3MCtxApplyPins: spec %d places 'at_frame' -- not yet "
                    "implemented; arrives with inside pins/repaint (a later "
                    "phase)." % i)
            if s.get("via"):
                raise ValueError(
                    "MAGIK_H3MCtxApplyPins: spec %d is a through-pin (resolved "
                    "via %s, the take that continues the clip it was asked "
                    "of). Two-stage mode pins from a clip or take "
                    "directly; through-pins at two resolutions arrive with "
                    "the Timeline's two-stage integration."
                    % (i, (s.get("via") or {}).get("source_id", "?")))
            if s.get("source_kind") == "clip_pixels":
                raise ValueError(
                    "MAGIK_H3MCtxApplyPins: spec %d pins from a clip with no "
                    "usable sidecar (encoded from its pixels), so there is "
                    "no exact full-resolution slice for stage 2. Two-stage "
                    "mode needs a take with its .mctx.safetensors." % i)

        target_video, _ = unpack_av(latent, name="latent")
        frames = fr.pixel_frames(int(target_video.shape[2]))
        lr_w = int(target_video.shape[4]) * 16
        lr_h = int(target_video.shape[3]) * 16
        if video_vae is None or audio_vae is None:
            raise ValueError(
                "MAGIK_H3MCtxApplyPins: two-stage mode builds the stage-1 pin "
                "by re-encoding the source file at %dx%d (pixel grade, "
                "Path C) -- connect video_vae and audio_vae."
                % (lr_w, lr_h))

        # R1: the exact slice; sidecar pins never use the encode size
        pins = _prepare_pins(pin_specs, snap_window_down_to_available,
                             ctx={"vae": video_vae, "audio_vae": audio_vae,
                                  "width": None, "height": None})
        n_before = sum(1 for p in pins if p.get("place") == "before")
        n_after = sum(1 for p in pins if p.get("place") == "after")
        if n_before > 1 or n_after > 1:
            raise ValueError(
                "MAGIK_H3MCtxApplyPins: at most one pin per side; got %d before "
                "/ %d after. (Several pins on the same side have no "
                "coordinate meaning.)" % (n_before, n_after))
        total_covered = sum(int(p["covered"]) for p in pins)
        if total_covered >= frames:
            raise ValueError(
                "MAGIK_H3MCtxApplyPins: pinning %d frames into a %d frame "
                "clip; the generated part must have room -- raise the clip "
                "length or shrink the pin window(s)."
                % (total_covered, frames))

        # R2/R3/R4: the stage-1 twin of each resolved pin, and the
        # run-time stage facts on the wire record
        lr_pins = []
        for i, pin in enumerate(pins):
            lr_pin = _dual_lr_pin(i, pin, pin_specs[i], lr_w, lr_h,
                                  video_vae, audio_vae)
            lr_pins.append(lr_pin)
            s = pin["spec"]
            hr_w, hr_h = int(pin["width"]), int(pin["height"])
            pin["stage_resolutions"] = [(lr_w, lr_h), (hr_w, hr_h)]
            pin["stage_frames"] = [frames, frames]
            pin["stage_grades"] = ["pixel", "latent"]
            _LOG.info(
                "magik.h3: TWO-STAGE %s-pin of %s: window %d..%d raw (%d of "
                "%d requested frames); stage 1 at %dx%d pixel-grade (file "
                "re-encode), stage 2 at %dx%d latent-grade (exact slice); "
                "shared audio %s",
                pin.get("place"), s.get("source_id") or "?",
                int(s.get("source_start", 0)),
                int(s.get("source_start", 0))
                + int(s.get("source_frames", 0)),
                int(s.get("source_frames", 0)),
                int(s.get("requested_window", 0)),
                lr_w, lr_h, hr_w, hr_h,
                ("%d steps, phase %+.2f, overhang %+.2f"
                 % (int(pin.get("audio_steps", 0) or 0),
                    float(pin.get("audio_phase", 0.0) or 0.0),
                    float(pin.get("overhang", 0.0) or 0.0)))
                if pin.get("audio") is not None else "off")

        bridge = (frames - total_covered if (n_before and n_after) else None)
        cond1, lat1 = self._stage_outputs(
            "stage 1: ", conditioning, latent, lr_pins, frames, lr_w, lr_h,
            masked_audio_feather, freeze_audio, audio_denoise, dynprompt,
            unique_id, 1, bridge)
        return (cond1, lat1, pins)


class MAGIK_H3MCtxApplyPinsStage2(MAGIK_H3MCtxApplyPins):
    """Stage 2 of the split dual-resolution flow.

    MAGIK_H3MCtxApplyPins with two_stage builds the stage-1 pair and
    leaves the exact full-resolution slices on the pins wire. This node
    takes those pins and the UPSCALED, SAMPLED stage-1 latent and builds
    the stage-2 pair from them: keyframe anchors and the mask at the
    stage-2 resolution, an inherited stage-1 mask stripped first. The
    first node's inputs never depend on the sampler's result and this
    node's inputs never depend on its own outputs, so the graph has no
    cycle.
    """
    CATEGORY = "magik/h3"
    FUNCTION = "apply"
    RETURN_TYPES = ("CONDITIONING", "LATENT", wt.PINS)
    RETURN_NAMES = ("conditioning", "latent", "pins")
    DESCRIPTION = (
        "Stage 2 of dual-resolution pinning. Takes the pins from H3 MCtx "
        "Apply Pins (two_stage on) and the upscaled latent the first "
        "sampler produced, and pins the same window at the high "
        "resolution -- the exact sidecar slice, no resize, no re-encode "
        "-- as keyframe anchors and/or a denoise mask for the second "
        "sampler. Wire the second sampler from latent, not from the "
        "upscaler."
    )
    OUTPUT_TOOLTIPS = (
        "Stage-2 conditioning with the pins anchored at the stage-2 "
        "resolution. Feed the second guider/sampler.",
        "The upscaled latent with the stage-2 mask written (any mask "
        "inherited from stage 1 stripped). ALWAYS wire the second "
        "sampler from here.",
        "The resolved pins, passed through. Feed MAGIK_H3TrimPinned / the "
        "Save nodes -- they read what was actually pinned.",
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "conditioning": ("CONDITIONING", {
                    "tooltip": "The stage-2 conditioning (the same "
                               "prompt/refs, built for the second "
                               "sampler), before any pins."}),
                "latent": ("LATENT", {
                    "tooltip": "The upscaled stage-1 result: spatial "
                               "upscale only, same length. Its "
                               "resolution must equal the pinned "
                               "clip's."}),
                "pins": (wt.PINS, {
                    "tooltip": "The pins output of H3 MCtx Apply Pins "
                               "(two_stage on)."}),
                "freeze_audio": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "As on Apply Pins: hold the whole audio "
                               "mask at 0 so the sound comes out as it "
                               "went in."}),
            },
            "optional": {
                "masked_audio_feather": ("INT", {
                    "default": 8, "min": 0, "max": 256,
                    "tooltip": "Masked pins only: half-cosine release of "
                               "the preserved audio (8 = 0.2 s)."}),
                "audio_denoise": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 1.0, "step": 0.05,
                    "tooltip": "With freeze_audio: hold the audio mask at "
                               "this value instead of 0 (see Apply "
                               "Pins)."}),
            },
            "hidden": {
                "dynprompt": "DYNPROMPT",
                "unique_id": "UNIQUE_ID",
            },
        }

    def apply(self, conditioning, latent, pins, freeze_audio=False,
              masked_audio_feather=8, audio_denoise=0.0,
              dynprompt=None, unique_id=None):
        # a mask that rode through the upscaler is stage 1's, at stage
        # 1's dims; stage 2 writes its own
        lat = self._strip_inherited_mask(latent)
        if not pins:
            _LOG.info("magik.h3: stage 2 has no pins; conditioning passes "
                      "through%s",
                      " and the latent carries an audio-only mask"
                      if freeze_audio else "")
            if freeze_audio:
                lat = nodes_masked.apply_masked_pins(
                    lat, [], masked_audio_feather, freeze_audio=True,
                    audio_denoise=audio_denoise)
            return (conditioning, lat, [])
        for i, pin in enumerate(pins):
            if not isinstance(pin, dict) or pin.get("video") is None:
                raise ValueError(
                    "MAGIK_H3MCtxApplyPinsStage2: pin %d is not a resolved "
                    "pin -- wire the pins output of H3 MCtx Apply Pins, "
                    "not a pin spec." % i)
        video, _ = unpack_av(lat, name="latent")
        frame_count = fr.pixel_frames(int(video.shape[2]))
        width = int(video.shape[4]) * 16
        height = int(video.shape[3]) * 16
        for i, pin in enumerate(pins):
            if (pin["width"], pin["height"]) != (width, height):
                raise ValueError(
                    "MAGIK_H3MCtxApplyPinsStage2: pin %d is the exact "
                    "slice of a %dx%d clip but the latent here is %dx%d. "
                    "A latent cannot be resized: upscale to the pinned "
                    "clip's resolution, or regenerate the source at the "
                    "size you want."
                    % (i, pin["width"], pin["height"], width, height))
            sf = pin.get("stage_frames")
            if sf and int(sf[0]) != frame_count:
                raise ValueError(
                    "MAGIK_H3MCtxApplyPinsStage2: stage 1 ran %d frames "
                    "but this latent is %d. The latent upscale must keep "
                    "the length (spatial only); the pins were placed on "
                    "the stage-1 timeline." % (int(sf[0]), frame_count))
            if not pin.get("stage_grades"):
                _LOG.info("magik.h3: pin %d did not come from a "
                          "two_stage Apply Pins; pinning its slice "
                          "as-is", i)
        n_before = sum(1 for p in pins if p.get("place") == "before")
        n_after = sum(1 for p in pins if p.get("place") == "after")
        if n_before > 1 or n_after > 1:
            raise ValueError(
                "MAGIK_H3MCtxApplyPinsStage2: at most one pin per side; got "
                "%d before / %d after." % (n_before, n_after))
        total_covered = sum(int(p["covered"]) for p in pins)
        if total_covered >= frame_count:
            raise ValueError(
                "MAGIK_H3MCtxApplyPinsStage2: pinning %d frames into a %d "
                "frame stage-2 clip; the generated part must have room."
                % (total_covered, frame_count))
        bridge = (frame_count - total_covered
                  if (n_before and n_after) else None)
        cond, lat_out = self._stage_outputs(
            "stage 2: ", conditioning, lat, pins, frame_count, width,
            height, masked_audio_feather, freeze_audio, audio_denoise,
            dynprompt, unique_id, 1, bridge)
        return (cond, lat_out, pins)


class MAGIK_H3TrimPinned:
    CATEGORY = "magik/h3"
    FUNCTION = "trim"
    RETURN_TYPES = ("IMAGE", "AUDIO")
    RETURN_NAMES = ("images", "audio")
    DESCRIPTION = (
        "Removes pinned scaffolding from a decoded clip, picture and "
        "sound together, and truncates the audio tail to exactly "
        "frames/fps (H3 rounds its audio grid up ~8 ms per clip; the "
        "error compounds at every join in a chain). Trim amounts come "
        "from the pins wire -- the same one that fed Apply."
    )
    OUTPUT_TOOLTIPS = (
        "The delivered frames. Feed MAGIK_H3SaveVideoWithMCtx.",
        "The delivered audio, duration-locked to the frames.",
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "pins": (wt.PINS, {
                    "tooltip": "The resolved pins from MAGIK_H3MCtxApplyPins. The "
                               "trim amounts are derived from what was "
                               "actually pinned: before-pins come off the "
                               "head, after-pins off the tail."}),
            },
            "optional": {
                "audio": ("AUDIO", {
                    "tooltip": "Decoded audio for the same clip; trimmed by "
                               "the same durations so sound stays locked to "
                               "picture, and tail-matched to exactly "
                               "frames/fps (H3 ships ~8 ms extra sound per "
                               "clip; the error compounds at joins). Leave "
                               "unwired for silent clips."}),
            },
        }

    def trim(self, images, pins, audio=None):
        head, tail = pins_trim_totals(pins)
        total = int(images.shape[0])
        if head + tail >= total:
            raise ValueError(
                "MAGIK_H3TrimPinned: trimming %d+%d frames from a %d frame clip "
                "leaves nothing." % (head, tail, total))
        out_images = images[head:total - tail] if (head or tail) else images
        remaining = total - head - tail

        out_audio = audio
        if audio is not None:
            waveform = audio["waveform"]
            sr = int(audio["sample_rate"])
            cut_head = int(round(head / float(fr.FPS) * sr))
            cut_tail = int(round(tail / float(fr.FPS) * sr))
            length = int(waveform.shape[-1])
            if cut_head + cut_tail >= length:
                raise ValueError(
                    "MAGIK_H3TrimPinned: trimming %.3fs+%.3fs from %.3fs of audio "
                    "leaves nothing. Wire the audio decoded from this same "
                    "clip." % (cut_head / sr, cut_tail / sr, length / sr))
            # Cut on the take's OWN frames: its sound sits `phase` audio
            # steps off them (a pin window that started between steps),
            # so the delivered sound starts that much earlier or later in
            # the decode. The latents keep it -- see pins_audio_phase.
            phase = pins_audio_phase(pins, total)
            shift = phase_samples(phase, sr)
            start = cut_head - shift
            # tail-match unconditionally: exactly one right answer (the
            # ~8 ms/clip audio surplus compounds at every join otherwise)
            want = int(round(remaining / float(fr.FPS) * sr))
            # never invent more than the shift itself past the decode
            end = min(start + want, length - cut_tail + abs(shift))
            if length - cut_head - cut_tail > want:
                _LOG.info("magik.h3: tail-matched audio, cut %d samples "
                          "(%.2f ms)", length - cut_head - cut_tail - want,
                          (length - cut_head - cut_tail - want) / sr * 1000.0)
            if shift:
                _LOG.info("magik.h3: delivered sound cut %.1f ms %s, on the "
                          "take's own frames (its pin window started "
                          "between two audio steps)", abs(shift) / sr * 1000.0,
                          "earlier" if shift > 0 else "later")
            waveform = shifted_samples(waveform, start, end)
            out_audio = {"waveform": waveform, "sample_rate": sr}
        elif head or tail:
            _LOG.info("magik.h3: trimmed %d head / %d tail frames with no "
                      "audio wired; if this clip has sound, trim it through "
                      "this node too or it will drift %.3fs.",
                      head, tail, (head + tail) / float(fr.FPS))
        return (out_images, out_audio)
