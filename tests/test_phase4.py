"""Phase 4 acceptance harness -- synthetic baseline pins.

The audit's Phase 4 acceptance experiment (ch. 11.7) needs a real
ComfyUI + MiniMax H3 run to produce its final numbers, but the
MEASUREMENT code and the ACCEPTANCE LOGIC can be exercised without a
model: the seam metric is latent-space, so a synthetic latent with the
right phase structure exercises the same measure_seam / verdict_for /
composition_drift code path a real run exercises.

These tests pin three things:

  the arms land in the right band
      control and test_path_c verdict "seamless" (< 1.45);
      ref_hard_cut verdicts "hard cut" (>= 1.9). This last one is the
      sanity check: if the metric could not detect a deliberately
      failed join, a "seamless" verdict would be meaningless.

  the acceptance logic matches the audit's two axes
      "must not regress seam metrics relative to control" -> verdict
      BAND, not numeric ratio (a pixel-grade seam is expected to sit a
      little higher than a clean handover but stay in the seamless
      band); "must measurably reduce first-stage composition drift" ->
      test.mean_drift < control.mean_drift - DRIFT_REDUCTION_MIN.

  the ordering is physical
      control.seam_ratio < test_path_c.seam_ratio < ref_hard_cut.seam_ratio
      (a clean handover reads lower than a pixel-grade shift reads lower
      than a hard cut); and test_path_c.composition_drift <
      control.composition_drift (the pin anchors generation).

Run from the pack folder:
  python tests/run_all.py
"""
import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMFY = os.path.abspath(os.path.join(ROOT, "..", ".."))
if COMFY not in sys.path:
    sys.path.insert(0, COMFY)
pack = sys.modules.get("magik_tl_test")
if pack is None:
    pack = types.ModuleType("magik_tl_test")
    pack.__path__ = [ROOT]
    sys.modules[pack.__name__] = pack

from magik_tl_test import phase4_synthetic as syn  # noqa: E402
from magik_tl_test import phase4_protocol as proto  # noqa: E402
from magik_tl_test.seam_report import SEAMLESS_BELOW, SOFT_BUMP_BELOW  # noqa: E402


class ArmsLandInTheRightBand(unittest.TestCase):
    """The three arms sit in the middle of each verdict band, matching
    the audit's calibration sample (clean 1.07-1.35, ambiguous
    1.55-1.75, hard cuts 1.99-2.77)."""

    def setUp(self):
        self.result = syn.run_synthetic()

    def test_control_is_seamless(self):
        arm = self.result["arms"]["control"]
        self.assertEqual(arm["verdict"], "seamless")
        self.assertLess(arm["seam_ratio"], SEAMLESS_BELOW)

    def test_test_path_c_is_seamless(self):
        arm = self.result["arms"]["test_path_c"]
        self.assertEqual(arm["verdict"], "seamless")
        self.assertLess(arm["seam_ratio"], SEAMLESS_BELOW)

    def test_ref_hard_cut_is_hard_cut(self):
        # the sanity check: if this fails, the metric cannot detect a
        # real failure and the "seamless" verdicts above are meaningless
        arm = self.result["arms"]["ref_hard_cut"]
        self.assertEqual(arm["verdict"], "hard cut")
        self.assertGreaterEqual(arm["seam_ratio"], SOFT_BUMP_BELOW)

    def test_synthetic_sanity_flag_passes(self):
        self.assertTrue(self.result["sanity_ref_hard_cut_verdicts_hard_cut"])


class AcceptanceLogicMatchesTheAudit(unittest.TestCase):
    """The audit's two-axis acceptance: verdict band not worse than
    control AND drift measurably reduced."""

    def setUp(self):
        self.result = syn.run_synthetic()

    def test_acceptance_passes_on_the_synthetic_baseline(self):
        acc = self.result["acceptance"]
        self.assertTrue(acc["accept"], acc["reasons"])

    def test_both_arms_are_seamless(self):
        arms = self.result["arms"]
        self.assertEqual(arms["control"]["verdict"], "seamless")
        self.assertEqual(arms["test_path_c"]["verdict"], "seamless")

    def test_test_arm_verdict_band_no_worse_than_control(self):
        arms = self.result["arms"]
        order = {"seamless": 0, "soft bump": 1, "hard cut": 2}
        self.assertLessEqual(
            order[arms["test_path_c"]["verdict"]],
            order[arms["control"]["verdict"]])

    def test_test_arm_reduces_drift_by_at_least_the_floor(self):
        arms = self.result["arms"]
        c = arms["control"]["composition_drift"]
        t = arms["test_path_c"]["composition_drift"]
        self.assertGreaterEqual(c - t, proto.DRIFT_REDUCTION_MIN)

    def test_a_test_arm_regression_is_caught(self):
        # if the test arm's verdict slipped to "soft bump", acceptance
        # must fail -- even if its drift is zero. This is the audit's
        # "must not regress seam metrics" axis.
        arms = self.result["arms"]
        control = arms["control"]
        bad_test = dict(arms["test_path_c"])
        bad_test["verdict"] = "soft bump"
        bad_test["seam_ratio"] = 1.6
        acc = proto.acceptance(control, bad_test)
        self.assertFalse(acc["accept"])
        self.assertTrue(any("seamless" in r for r in acc["reasons"]))

    def test_a_drift_regression_is_caught(self):
        # if the test arm's drift matched control's, acceptance must
        # fail -- even if its seam verdict is seamless. This is the
        # audit's "must measurably reduce drift" axis.
        arms = self.result["arms"]
        control = arms["control"]
        bad_test = dict(arms["test_path_c"])
        bad_test["composition_drift"] = control["composition_drift"]
        acc = proto.acceptance(control, bad_test)
        self.assertFalse(acc["accept"])
        self.assertTrue(any("drift" in r for r in acc["reasons"]))


class OrderingIsPhysical(unittest.TestCase):
    """The arms separate on both axes the way the audit's two-axis
    acceptance is meant to tell apart."""

    def setUp(self):
        self.result = syn.run_synthetic()

    def test_seam_ratio_orders_clean_below_pixel_below_hard_cut(self):
        arms = self.result["arms"]
        self.assertLess(arms["control"]["seam_ratio"],
                        arms["test_path_c"]["seam_ratio"])
        self.assertLess(arms["test_path_c"]["seam_ratio"],
                        arms["ref_hard_cut"]["seam_ratio"])

    def test_test_arm_drift_is_below_control_drift(self):
        arms = self.result["arms"]
        self.assertLess(arms["test_path_c"]["composition_drift"],
                        arms["control"]["composition_drift"])


class Determinism(unittest.TestCase):
    """Same seed -> same numbers. The synthetic baseline is a fixed
    reference point; a different number across runs would mean either
    the seed broke or the metric itself is non-deterministic."""

    def test_two_runs_are_identical(self):
        a = syn.run_synthetic()
        b = syn.run_synthetic()
        self.assertEqual(
            a["arms"]["test_path_c"]["seam_ratio"],
            b["arms"]["test_path_c"]["seam_ratio"])
        self.assertEqual(
            a["arms"]["control"]["composition_drift"],
            b["arms"]["control"]["composition_drift"])


if __name__ == "__main__":
    unittest.main()
