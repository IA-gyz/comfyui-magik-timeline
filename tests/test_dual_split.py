"""Two-stage dual resolution: the two nodes and why the graph has no cycle.

  MAGIK_H3MCtxApplyPins(two_stage=True)   -> stage-1 pair + exact
                                             full-res slices on `pins`
  sampler 1 -> latent upscale (spatial only)
  MAGIK_H3MCtxApplyPinsStage2(pins, upscaled latent) -> stage-2 pair

Pins: stage 1 takes no stage-2 input; the stage-2 size comes from the
pinned clip; stage 2 refuses a wrong resolution, a changed length, an
unwired latent output and non-pins input; an inherited mask is
stripped; pins pass through; the recipe is untouched.
"""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_dual_resolution as base  # noqa: E402

torch = base.torch
avpack, fr, nodes_pins = base.avpack, base.fr, base.nodes_pins
WIN_STEPS, STEPS, FRAMES = base.WIN_STEPS, base.STEPS, base.FRAMES


def stage1(specs, latent, vae=object(), freeze=False, **kw):
    return nodes_pins.MAGIK_H3MCtxApplyPins()._apply(
        base.cond(), latent, False, freeze, specs, vae, vae, 8, 0.0,
        two_stage=True, **kw)


def stage2(pins, latent, freeze=False, **kw):
    return nodes_pins.MAGIK_H3MCtxApplyPinsStage2().apply(
        base.cond(), latent, pins, freeze, **kw)


class SplitFixture(base.DualFixture):
    def split(self, mode="masked", **kw):
        b = base.bundle(self_id=self.file_id)
        return stage1(base.spec_of(b, mode=mode), base.av_target(w=3, h=2),
                      **kw)


class Stage1NeedsNoStage2Latent(SplitFixture):
    def test_stage1_builds_without_a_stage2_latent(self):
        c1, l1, pins = self.split()
        self.assertIsNotNone(c1)
        self.assertEqual(len(pins), 1)

    def test_stage2_size_comes_from_the_pinned_clip(self):
        (_, _, (p,)) = self.split()
        self.assertEqual(p["stage_resolutions"], [(48, 32), (96, 64)])
        self.assertEqual(p["stage_grades"], ["pixel", "latent"])
        self.assertEqual(p["stage_frames"], [FRAMES, FRAMES])

    def test_stage1_pin_is_still_pixel_grade_same_interval(self):
        self.split()
        firsts = self.encoded["images"][:, 0, 0, 0].tolist()
        self.assertEqual(firsts,
                         [1000 + f for f in range(FRAMES - base.WIN, FRAMES)])

    def test_only_the_stage1_latent_must_be_wired(self):
        out = self.split(dynprompt=base.FakeDyn(["uid", 1]),
                         unique_id="uid")
        self.assertEqual(len(out), 3)
        with self.assertRaisesRegex(ValueError, "latent output"):
            self.split(dynprompt=base.FakeDyn(), unique_id="uid")

    def test_recipe_is_untouched(self):
        (_, _, pins) = self.split()
        import magik_tl_test.mctx as mctx
        for rec in mctx.serialize_pins(pins):
            self.assertNotIn("stage_grades", rec)
            self.assertNotIn("stage_frames", rec)
            self.assertEqual(rec["source_kind"], "clip")


class Stage1Refusals(SplitFixture):
    def test_pixel_source_is_refused(self):
        b = base.bundle(self_id=self.file_id)
        specs = copy.deepcopy(base.spec_of(b))
        specs[0]["source_kind"] = "clip_pixels"
        with self.assertRaisesRegex(ValueError, "no usable sidecar"):
            stage1(specs, base.av_target(w=3, h=2))

    def test_no_specs_passes_through(self):
        out = stage1([], base.av_target(w=3, h=2))
        self.assertEqual(out[2], [])
        self.assertEqual(len(out), 3)


class Stage2(SplitFixture):
    def run_stage2(self, mode="masked", latent=None, **kw):
        _, _, pins = self.split(mode=mode)
        return pins, stage2(pins, latent if latent is not None
                            else base.av_target(w=6, h=4), **kw)

    def test_mask_and_slice_at_the_stage2_resolution(self):
        pins, (c, lat, out_pins) = self.run_stage2()
        v, _ = avpack.unpack_av(lat)
        start = ((FRAMES - base.WIN) // fr.FRAMES_PER_GROUP) \
            * fr.LATENTS_PER_GROUP
        self.assertEqual(v[0, 0, :WIN_STEPS, 0, 0].tolist(),
                         list(range(start, start + WIN_STEPS)))
        m = lat["noise_mask"].tensors[0]
        self.assertEqual(tuple(m.shape), (1, 1, STEPS, 4, 6))
        self.assertEqual(m[0, 0, :WIN_STEPS].flatten().tolist(),
                         [0.0] * (WIN_STEPS * 4 * 6))
        self.assertEqual(float(m[0, 0, -1, 0, 0]), 1.0)

    def test_pins_pass_through_unchanged(self):
        pins, (_, _, out_pins) = self.run_stage2()
        self.assertIs(out_pins, pins)

    def test_inherited_stage1_mask_is_stripped(self):
        up = base.av_target(w=6, h=4)
        up["noise_mask"] = avpack.pack_av(
            torch.zeros(1, 4, STEPS, 2, 3),
            torch.zeros(1, 4, 2, fr.audio_total(FRAMES)))["samples"]
        _, (_, lat, _) = self.run_stage2(latent=up)
        self.assertEqual(
            tuple(lat["noise_mask"].tensors[0].shape), (1, 1, STEPS, 4, 6))

    def test_wrong_resolution_is_refused(self):
        with self.assertRaisesRegex(ValueError, "cannot be resized"):
            self.run_stage2(latent=base.av_target(w=3, h=2))

    def test_changed_length_is_refused(self):
        with self.assertRaisesRegex(ValueError, "ran %d frames" % FRAMES):
            self.run_stage2(latent=base.av_target(w=6, h=4, frames=57))

    def test_a_spec_instead_of_pins_is_refused(self):
        specs = base.spec_of(base.bundle(self_id=self.file_id))
        with self.assertRaisesRegex(ValueError, "not a resolved pin"):
            stage2(specs, base.av_target(w=6, h=4))

    def test_unwired_latent_is_refused(self):
        with self.assertRaisesRegex(ValueError, "latent output"):
            self.run_stage2(dynprompt=base.FakeDyn(), unique_id="uid")

    def test_wired_latent_passes(self):
        pins, out = self.run_stage2(dynprompt=base.FakeDyn(["uid", 1]),
                                    unique_id="uid")
        self.assertEqual(len(out), 3)

    def test_no_pins_passes_through(self):
        up = base.av_target(w=6, h=4)
        c, lat, pins = stage2([], up)
        self.assertEqual(pins, [])
        self.assertIs(lat, up)


class NoCycle(unittest.TestCase):
    def test_the_nodes_ask_for_nothing_downstream_of_themselves(self):
        # Stage 1 has no latent input beyond its own target; stage 2's
        # latent input is the UPSCALED sampler-1 result, which is fine
        # because stage 2 is a separate node downstream of stage 1.
        t1 = nodes_pins.MAGIK_H3MCtxApplyPins
        for section in t1.INPUT_TYPES().values():
            self.assertNotIn("target_latent_hr", section)
        self.assertIn("two_stage", t1.INPUT_TYPES()["optional"])
        self.assertEqual(len(t1.RETURN_TYPES), 3)
        t2 = nodes_pins.MAGIK_H3MCtxApplyPinsStage2
        self.assertEqual(set(t2.INPUT_TYPES()["required"]),
                         {"conditioning", "latent", "pins", "freeze_audio"})
        self.assertEqual(len(t2.RETURN_TYPES), 3)


if __name__ == "__main__":
    unittest.main()
