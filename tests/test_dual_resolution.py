"""Dual-resolution mode: one pin window, two representations.

The audit's rules R1-R7 (Magik_Timeline_Upstream_Audit.pdf, ch. 11.6),
implemented as two nodes (MAGIK_H3MCtxApplyPins with two_stage, then
MAGIK_H3MCtxApplyPinsStage2 on the upscaled sampled latent):

  one record   the window is resolved ONCE, against the stage-2 target,
               through the unchanged latent slicer; the stage-1 encode
               is driven by the resolved record and verified to cover
               the same file interval (it never re-snaps);
  two grades   stage 2 pins the exact sidecar slice (latent grade),
               stage 1 pins a re-encode of the source FILE at its own
               resolution (pixel grade, Path C);
  shared audio one slice, one set of timing numbers, both stages;
  per stage    conditioning and masks are built per stage from the
               pristine conditioning, and an inherited stage-1 mask is
               stripped from the stage-2 target before a fresh one is
               written;
  untouched    single-resolution mode returns exactly what it always
               returned (plus two Nones), and nothing stage-related
               can reach the sidecar recipe.

The stage-1 encode and the file reads are stood in for, the way
test_short_take stands the pixel route in: which frames, of which
file, in what order. Runs against the real modules with ComfyUI on
sys.path. Run from the pack folder:
  python tests/run_all.py
"""
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import torch
from safetensors.torch import save_file

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMFY = os.path.abspath(os.path.join(ROOT, "..", ".."))
if COMFY not in sys.path:
    sys.path.insert(0, COMFY)
pack = sys.modules.get("magik_tl_test")
if pack is None:
    pack = types.ModuleType("magik_tl_test")
    pack.__path__ = [ROOT]
    sys.modules[pack.__name__] = pack
import folder_paths  # noqa: E402
from magik_tl_test import avpack  # noqa: E402
from magik_tl_test import frames as fr  # noqa: E402
from magik_tl_test import mctx  # noqa: E402
from magik_tl_test import nodes_encode as ne  # noqa: E402
from magik_tl_test import nodes_load  # noqa: E402
from magik_tl_test import nodes_masked  # noqa: E402
from magik_tl_test import nodes_pins  # noqa: E402

FRAMES = 141                       # on the 17k+5 grid; 42 video steps
STEPS = fr.frames_to_latents(FRAMES)
WIN = 39                           # a window on the shared AV grid
WIN_STEPS = fr.steps_for_frames(WIN)
SR = 32000


def bundle(self_id="SRC", head=0, delivered=FRAMES, w=6, h=4,
           clip="proj/src.mp4"):
    """A fake MCTX bundle: tagged video (step index) and tagged audio
    (tick index), so slices are recognisable by value."""
    raw = head + delivered
    video = torch.arange(raw and fr.frames_to_latents(raw),
                         dtype=torch.float32)
    video = video.view(1, 1, -1, 1, 1).expand(1, 4, -1, h, w).clone()
    ticks = round(raw / 24 * 40)
    audio = torch.arange(ticks, dtype=torch.float32)
    audio = audio.view(1, 1, 1, -1).expand(1, 4, 2, -1).clone()
    meta = {"raw_frames": str(raw), "delivered_frames": str(delivered),
            "pinned_head_frames": str(head), "pinned_tail_frames": "0",
            "pins": "[]"}
    return {"self_id": self_id, "clip": clip, "meta": meta,
            "video_latent": video, "audio_latent": audio,
            "origin": "sampled"}


def av_target(w=6, h=4, frames=FRAMES):
    """An empty AV target latent at `w`x`h` latent cells."""
    video = torch.zeros(1, 4, fr.frames_to_latents(frames), h, w)
    audio = torch.zeros(1, 4, 2, fr.audio_total(frames))
    return avpack.pack_av(video, audio)


def cond():
    return [(torch.zeros(1, 1, 1), {})]


def spec_of(b, mode="masked", window=WIN):
    (specs,) = nodes_pins.MAGIK_H3MCtxPinSpec().build(
        mctx=b, window=str(window), take_from="tail", take_from_frame=0,
        place="before", place_at_frame=0, audio_window=0, mode=mode,
        mask_ramp_frames=0, mask_ramp_edge=0.0, mask_hold=0.0,
        pin_specs=None)
    return specs


def apply_pins(pin_specs, latent, vae=object(), freeze=False,
               dynprompt=None, unique_id=None, two_stage=False):
    return nodes_pins.MAGIK_H3MCtxApplyPins()._apply(
        cond(), latent, False, freeze, pin_specs, vae, vae, 8, 0.0,
        dynprompt, unique_id, two_stage)


def apply_stage2(pins, latent, freeze=False, dynprompt=None,
                 unique_id=None):
    return nodes_pins.MAGIK_H3MCtxApplyPinsStage2().apply(
        cond(), latent, pins, freeze, dynprompt=dynprompt,
        unique_id=unique_id)


class FakeDyn:
    """The shape _output_wired scans: one input per link tuple, the way
    real nodes carry their upstream links."""

    def __init__(self, *links):
        self.links = list(links)

    def all_node_ids(self):
        return ["n1"]

    def get_node(self, _nid):
        return {"inputs": {"in%d" % i: l for i, l in enumerate(self.links)}}


class DualFixture(unittest.TestCase):
    """Output folder, a real clip file + sidecar, the pixel encode and
    the file reads stood in for."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="magik_tl_dual_")
        self._old_out = folder_paths.get_output_directory()
        folder_paths.set_output_directory(self.root)
        nodes_load._HASH_CACHE.clear()
        self.video_path = os.path.join(self.root, "proj", "src.mp4")
        os.makedirs(os.path.dirname(self.video_path), exist_ok=True)
        with open(self.video_path, "wb") as f:
            f.write(b"source frames")
        self.file_id = mctx.hash_file(self.video_path)
        save_file({"video": torch.zeros(1)}, mctx.sidecar_path(self.video_path),
                  metadata={"format": mctx.FORMAT, "self_id": self.file_id,
                            "relation": "", "parent_id": "", "pins": "[]"})
        self.saved_pixel = (ne.probe_clip, ne.decode_window,
                            ne._decode_audio_range, ne.encode_bundle)
        self.stub_pixels()
        # The checkout carries core's per-stream H3 masks (PR #15375);
        # this sandbox just cannot import comfy.model_base to see them
        # (torchvision build mismatch), so the availability check is
        # pinned True here and tested False explicitly in Refusals.
        self._masks = patch.object(nodes_masked, "core_masks_available",
                                   lambda: True)
        self._masks.start()

    def tearDown(self):
        self._masks.stop()
        (ne.probe_clip, ne.decode_window, ne._decode_audio_range,
         ne.encode_bundle) = self.saved_pixel
        folder_paths.set_output_directory(self._old_out)
        shutil.rmtree(self.root, ignore_errors=True)

    def stub_pixels(self, frames=FRAMES, tag=1000):
        """The file decodes to pixels of value tag + frame index; the
        encode records what it was handed and returns a bundle of the
        asked-for dimensions."""
        self.encoded = {}

        def probe(path):
            return {"frames": frames, "fps": 24.0, "src_frames": frames,
                    "width": 64, "height": 32}

        def decode(path, start, count, info=None):
            img = torch.zeros(count, 32, 64, 3)
            for k in range(count):
                img[k] = tag + start + k
            return img, {"waveform": torch.zeros(1, 1, round(count / 24 * SR)),
                         "sample_rate": SR}

        def encode(images, vae, audio_vae, **kw):
            self.encoded["images"] = images
            self.encoded["kw"] = kw
            n = int(images.shape[0])
            w, h = int(kw["width"]), int(kw["height"])
            return {"self_id": "enc", "origin": "encoded",
                    "meta": {"raw_frames": str(n),
                             "delivered_frames": str(n),
                             "pinned_head_frames": "0"},
                    "video_latent": torch.zeros(1, 4, fr.frames_to_latents(n),
                                                h // 16, w // 16),
                    "audio_latent": torch.zeros(1, 4, 2, fr.audio_total(n))}

        ne.probe_clip, ne.decode_window = probe, decode
        ne._decode_audio_range = (
            lambda *a, **k: {"waveform": torch.zeros(1, 1, 8),
                             "sample_rate": SR})
        ne.encode_bundle = encode

    def dual(self, b=None, mode="masked", target=None, freeze=False,
             dyn1=None, dyn2=None):
        """Stage 1 on a 3x2-cell latent, then stage 2 on `target` (the
        'upscaled' latent, 6x4 cells by default); returns the pair of
        stages as (cond1, lat1, pins, cond2, lat2)."""
        b = b or bundle(self_id=self.file_id)
        specs = spec_of(b, mode=mode)
        c1, l1, pins = apply_pins(
            specs, av_target(w=3, h=2), freeze=freeze, two_stage=True,
            dynprompt=dyn1, unique_id="uid" if dyn1 is not None else None)
        c2, l2, _ = apply_stage2(
            pins, target if target is not None else av_target(),
            freeze=freeze, dynprompt=dyn2,
            unique_id="uid" if dyn2 is not None else None)
        return c1, l1, pins, c2, l2


class OneRecordTwoRepresentations(DualFixture):
    def test_dual_mode_returns_two_stages(self):
        out = self.dual()
        self.assertEqual(len(out), 5)
        c1, l1, pins, c2, l2 = out
        self.assertIsNotNone(c2)
        self.assertIsNotNone(l2)
        # the wire carries ONE record: the stage-2 (authoritative) one
        self.assertEqual(len(pins), 1)
        p = pins[0]
        self.assertEqual(p["spec"]["source_kind"], "clip")
        self.assertEqual(p["covered"], WIN)
        self.assertEqual((p["spec"]["source_start"],
                          p["spec"]["source_frames"]), (FRAMES - WIN, WIN))

    def test_the_stage_facts_ride_the_wire(self):
        (_, _, (p,), _, _) = self.dual()
        self.assertEqual(p["stage_resolutions"], [(48, 32), (96, 64)])
        self.assertEqual(p["stage_grades"], ["pixel", "latent"])

    def test_both_stages_cover_the_same_interval(self):
        self.dual()
        # the encode was driven by the resolved record: exactly the
        # window's frames, of the source file, at the stage-1 size
        firsts = self.encoded["images"][:, 0, 0, 0].tolist()
        self.assertEqual(firsts,
                         [1000 + f for f in range(FRAMES - WIN, FRAMES)])
        self.assertEqual((self.encoded["kw"]["width"],
                          self.encoded["kw"]["height"]), (48, 32))

    def test_stage2_video_is_the_exact_slice(self):
        (_, l1, _, _, l2) = self.dual()
        v1, _ = avpack.unpack_av(l1)
        v2, _ = avpack.unpack_av(l2)
        # stage 2 holds the source's own steps: raw 102 = step 6*5 = 30
        start = ((FRAMES - WIN) // fr.FRAMES_PER_GROUP) * fr.LATENTS_PER_GROUP
        self.assertEqual(start, 30)
        self.assertEqual(v2[0, 0, :WIN_STEPS, 0, 0].tolist(),
                         list(range(start, start + WIN_STEPS)))
        # stage 1 holds the (zero-valued) re-encoded window at the LR
        # target's cell grid; stage 2 at the HR target's
        self.assertEqual(v1.shape[3], 2)
        self.assertEqual(v2.shape[3], 4)

    def test_masks_are_written_per_stage(self):
        (_, l1, _, _, l2) = self.dual()
        m1 = l1["noise_mask"].tensors[0]
        m2 = l2["noise_mask"].tensors[0]
        # a full-latent mask, with the pinned window's steps held (0)
        self.assertEqual(tuple(m1.shape), (1, 1, STEPS, 2, 3))
        self.assertEqual(tuple(m2.shape), (1, 1, STEPS, 4, 6))
        self.assertEqual(m1[0, 0, :WIN_STEPS].flatten().tolist(),
                         [0.0] * (WIN_STEPS * 2 * 3))
        self.assertEqual(m2[0, 0, :WIN_STEPS].flatten().tolist(),
                         [0.0] * (WIN_STEPS * 4 * 6))
        self.assertEqual(float(m1[0, 0, -1, 0, 0]), 1.0)   # rest generates
        self.assertEqual(float(m2[0, 0, -1, 0, 0]), 1.0)


class AudioShared(DualFixture):
    def test_one_slice_serves_both_stages(self):
        (_, l1, (p,), _, l2) = self.dual()
        span = fr.audio_total(WIN)      # 65 ticks
        _, a1 = avpack.unpack_av(l1)
        _, a2 = avpack.unpack_av(l2)
        want = torch.arange(fr.audio_total(FRAMES) - span,
                            fr.audio_total(FRAMES))
        # both stages received the source's own audio latent slice
        self.assertTrue(torch.equal(a1[0, 0, 0, :span], want))
        self.assertTrue(torch.equal(a2[0, 0, 0, :span], want))
        self.assertEqual(p["audio_steps"], span)
        self.assertEqual(p["spec"]["audio_start_step"], 170)

    def test_the_timing_is_metadata_on_the_record(self):
        (_, _, (p,), _, _) = self.dual()
        self.assertEqual(float(p["audio_phase"]), 0.0)
        self.assertEqual(float(p["overhang"]), 0.0)


class Stage2MaskStripped(DualFixture):
    def test_an_inherited_mask_is_replaced_not_composed(self):
        target = av_target()
        target["noise_mask"] = avpack.pack_av(
            torch.zeros(1, 4, STEPS, 4, 6),
            torch.zeros(1, 4, 2, fr.audio_total(FRAMES)))["samples"]
        # upstream refuses to compose masks; the dual node strips the
        # inherited one and writes the fresh HR mask
        (_, _, _, _, l2) = self.dual(target=target)
        m2 = l2["noise_mask"].tensors[0]
        self.assertEqual(tuple(m2.shape), (1, 1, STEPS, 4, 6))
        self.assertEqual(m2[0, 0, :WIN_STEPS].flatten().tolist(),
                         [0.0] * (WIN_STEPS * 4 * 6))

    def test_a_clean_target_is_passed_through_when_guide_only(self):
        # masked mode always writes a fresh mask; with nothing to hold,
        # the stage-2 latent is the node's input object itself
        target = av_target()
        out = self.dual(mode="guide", target=target)
        self.assertIs(out[4], target)


class WiringChecks(DualFixture):
    def test_unwired_stage1_latent_is_refused(self):
        with self.assertRaisesRegex(ValueError, "latent output"):
            self.dual(dyn1=FakeDyn())

    def test_unwired_stage2_latent_is_refused(self):
        with self.assertRaisesRegex(ValueError, "latent output"):
            self.dual(dyn1=FakeDyn(["uid", 1]), dyn2=FakeDyn())

    def test_both_wired_passes(self):
        out = self.dual(dyn1=FakeDyn(["uid", 1]), dyn2=FakeDyn(["uid", 1]))
        self.assertEqual(len(out), 5)


class StandardMode(DualFixture):
    def test_single_resolution_returns_the_three_outputs(self):
        b = bundle(self_id=self.file_id)
        specs = spec_of(b)
        out = apply_pins(specs, av_target())
        self.assertEqual(len(out), 3)
        (p,) = out[2]
        self.assertNotIn("stage_resolutions", p)
        self.assertNotIn("stage_grades", p)

    def test_single_resolution_does_not_touch_the_file(self):
        b = bundle(self_id=self.file_id)
        apply_pins(spec_of(b), av_target())
        self.assertEqual(self.encoded, {})     # no encode happened

    def test_standard_pins_match_a_direct_prepare(self):
        b = bundle(self_id=self.file_id)
        specs = spec_of(b)
        out = apply_pins(specs, av_target())
        (direct,) = nodes_pins._prepare_pins(specs, False)
        self.assertEqual(out[2][0]["covered"], direct["covered"])
        self.assertEqual(out[2][0]["spec"]["source_start"],
                         direct["spec"]["source_start"])
        self.assertTrue(torch.equal(out[2][0]["video"], direct["video"]))


class GuideMode(DualFixture):
    def test_keyframes_per_stage_at_each_resolution(self):
        c1, l1, _, c2, l2 = self.dual(mode="guide")
        # one VIDEO row and one AUDIO row per stage's pin
        (k1,) = [k for k in c1[0][1]["minimax_keyframes"] if "latent" in k]
        (k2,) = [k for k in c2[0][1]["minimax_keyframes"] if "latent" in k]
        self.assertEqual(tuple(k1["latent"].shape), (1, 4, WIN_STEPS, 2, 3))
        self.assertEqual(tuple(k2["latent"].shape), (1, 4, WIN_STEPS, 4, 6))
        start = ((FRAMES - WIN) // fr.FRAMES_PER_GROUP) * fr.LATENTS_PER_GROUP
        self.assertEqual(k2["latent"][0, 0, :, 0, 0].tolist(),
                         list(range(start, start + WIN_STEPS)))
        # guide mode writes no masks
        self.assertNotIn("noise_mask", l1)
        self.assertNotIn("noise_mask", l2)


class Refusals(DualFixture):
    def test_masks_unavailable_is_refused(self):
        with patch.object(nodes_masked, "core_masks_available", lambda: False):
            with self.assertRaisesRegex(ValueError, "per-stream H3 denoise"):
                self.dual()

    def test_through_pins_are_refused(self):
        b = bundle(self_id=self.file_id)
        spec = spec_of(b)[0]
        spec["via"] = {"source_id": "A", "raw_offset": 0}
        with self.assertRaisesRegex(ValueError, "through-pin"):
            apply_pins([spec], av_target(w=3, h=2), vae=object(),
                       two_stage=True)

    def test_missing_vaes_are_refused(self):
        b = bundle(self_id=self.file_id)
        with self.assertRaisesRegex(ValueError, "video_vae and audio_vae"):
            apply_pins(spec_of(b), av_target(w=3, h=2), vae=None,
                       two_stage=True)

    def test_a_stage2_resolution_mismatch_is_refused(self):
        with self.assertRaisesRegex(ValueError, "cannot be resized"):
            self.dual(target=av_target(w=3, h=2))

    def test_a_changed_source_file_is_refused(self):
        # the bundle's identity no longer matches the file's hash
        with self.assertRaisesRegex(ValueError, "hashes to"):
            self.dual(b=bundle(self_id="stale"))

    def test_a_window_too_long_for_the_stage_is_refused(self):
        b = bundle(self_id=self.file_id)
        with self.assertRaisesRegex(ValueError, "must have room"):
            apply_pins(spec_of(b, window=124), av_target(w=3, h=2, frames=57),
                       vae=object(), two_stage=True)


class NoPins(DualFixture):
    def test_freeze_covers_both_stages(self):
        c1, l1, pins = apply_pins([], av_target(w=3, h=2), freeze=True,
                                  two_stage=True)
        c2, l2, pins2 = apply_stage2(pins, av_target(), freeze=True)
        self.assertEqual(pins, [])
        self.assertEqual(pins2, [])
        self.assertIn("noise_mask", l1)
        self.assertIn("noise_mask", l2)

    def test_without_freeze_both_stages_pass_through(self):
        _, l1, pins = apply_pins([], av_target(w=3, h=2), two_stage=True)
        up = av_target()
        _, l2, _ = apply_stage2(pins, up)
        self.assertNotIn("noise_mask", l1)
        self.assertIs(l2, up)

    def test_the_inherited_mask_is_stripped_even_without_pins(self):
        up = av_target()
        up["noise_mask"] = up["samples"]
        _, l2, _ = apply_stage2([], up, freeze=True)
        # stage 2 re-wrote a fresh mask rather than composing with the
        # inherited one (which would have been refused downstream)
        self.assertIn("noise_mask", l2)


class SidecarUntouched(DualFixture):
    def test_the_stage_facts_cannot_reach_the_recipe(self):
        (_, _, (p,), _, _) = self.dual()
        rows = json.loads(mctx.serialize_pins([p["spec"]]))
        self.assertEqual(len(rows), 1)
        self.assertNotIn("stage_resolutions", rows[0])
        self.assertNotIn("stage_grades", rows[0])
        # and the recipe still names the source the way a
        # single-resolution take does: the clip, in raw frames
        self.assertEqual(rows[0]["source_kind"], "clip")
        self.assertEqual((rows[0]["source_start"], rows[0]["source_frames"]),
                         (FRAMES - WIN, WIN))

    def test_lineage_grade_stays_latent(self):
        (_, _, (p,), _, _) = self.dual()
        self.assertEqual(mctx.lineage_grade([p["spec"]]), "latent")


if __name__ == "__main__":
    unittest.main()
