# Magik Timeline -- Phase 4 acceptance experiment
# (GPL-3.0). Provenance: docs/MAGIK_PORT.md sec. 5; audit ch. 11.7.
"""Phase 4 harness for real ComfyUI outputs.

Loads takes from two folders (control and test arms), measures each
take's seam and drift, aggregates per arm, and applies the acceptance
criteria from ``phase4_protocol``.

Each folder should contain takes saved by ``MAGIK_H3SaveVideoWithMCtx``
or ``MAGIK_H3SaveMCtxForVideo``: one ``.safetensors`` sidecar per take
(the ``mctx_v1`` file), plus the ``.mp4`` the saver wrote. The harness
measures the latent-space seam on the sidecar's video latent (via
``seam_report.measure_seam``) and the composition drift from the take's
own pinned-vs-generated structure (via ``phase4_synthetic.composition_drift``).
When the ``.mp4`` is present it also runs the frame-difference cut scan
(via ``seam_report.measure_cuts``), which catches the single-frame jump
cuts the latent metric cannot see.

The drift metric here is an APPROXIMATION of the audit's "first-stage
composition drift on the held-out windows": the audit's drift is
measured against the SOURCE window's own latent (the parent's pinned
slice), while this harness measures it against the take's OWN pinned
span. For a take that faithfully reproduces its pin (which is what
``measure_seam``'s fidelity column checks in ``seam_report.report``)
the two are close; for a take that drifts, the take's-own-span drift is
a lower bound on the against-source drift. The harness reports both the
seam fidelity (cosine similarity between the pinned span and the parent
slice, when the parent is in the same folder) and the drift, so the
operator can flag a take whose own-span drift reads low but whose
fidelity is also low (the pin didn't hold, so the drift metric is
uninformative).

Run from the pack folder:

    python -m phase4_run \\
        --control /path/to/control_takes \\
        --test    /path/to/test_takes \\
        --out     phase4_result.json

or just the synthetic baseline (no model, no GPU, no folders):

    python -m phase4_run --synthetic
"""
import argparse
import json
import os
import sys

try:
    from seam_report import measure_seam, measure_cuts, verdict_for
    from mctx import load_sidecar, parse_pins, read_header, SIDECAR_SUFFIX
    import frames as fr
    import phase4_protocol as protocol
    from phase4_synthetic import (composition_drift, run_synthetic,
                                  _print_report)
except ImportError:                             # pragma: no cover
    from .seam_report import measure_seam, measure_cuts, verdict_for
    from .mctx import load_sidecar, parse_pins, read_header, SIDECAR_SUFFIX
    from . import frames as fr
    from . import phase4_protocol as protocol
    from .phase4_synthetic import (composition_drift, run_synthetic,
                                    _print_report)

import torch


def _pinned_window(meta):
    """The pinned window's frame count, per seam_report.report's logic."""
    def _sum(place, fallback):
        n = sum(int(s.get("source_frames", 0) or 0)
                for s in parse_pins(meta)
                if s.get("place") == place)
        return n or int(meta.get(fallback, 0))
    return _sum("before", "pinned_head_frames"), _sum("after", "pinned_tail_frames")


def measure_take(sidecar_path):
    """Measure one take: seam ratio, verdict, drift, cut scan.

    Returns a dict, or None if the sidecar is not an extend/prepend
    (root clips have no seam to measure).
    """
    meta = read_header(sidecar_path)
    rel = meta.get("relation")
    if rel not in ("extends", "prepends"):
        return None
    video, _audio, meta = load_sidecar(sidecar_path)
    video = video.float()
    head, tail = _pinned_window(meta)
    lt = video.shape[2]
    if rel == "extends":
        seam = fr.frames_to_latents(head) - 1
        into = +1
    else:                                   # prepends
        seam = lt - fr.frames_to_latents(tail) - 1
        into = -1
    m = measure_seam(video, seam, into)
    ratio = m["ratio"] if m else float("nan")
    drift = composition_drift(video, seam)
    # cut scan on the delivered MP4, when present
    mp4 = sidecar_path[:-len(SIDECAR_SUFFIX)] + ".mp4"
    cuts = None
    if os.path.isfile(mp4):
        try:
            cuts = measure_cuts(mp4, where="end" if rel == "prepends"
                                else "start")
        except Exception as exc:
            cuts = {"error": "%s: %s" % (type(exc).__name__, exc)}
    return {
        "clip": os.path.basename(sidecar_path).split(".")[0],
        "relation": rel,
        "seam_ratio": round(ratio, 4),
        "verdict": verdict_for(ratio),
        "at_frames": round(m["at_frames"], 3) if m else 0.0,
        "boundary_ratio": round(m["boundary"], 4) if m else 0.0,
        "composition_drift": round(drift, 4),
        "cut_scan": ({"ratio": round(cuts["ratio"], 4),
                      "verdict": cuts["verdict"],
                      "kind": cuts["kind"],
                      "at": cuts["at"]}
                     if cuts and "ratio" in cuts else cuts),
        "pinned_head": head,
        "pinned_tail": tail,
        "latent_steps": lt,
    }


def measure_folder(folder):
    """Measure every take in a folder. Returns an arm-result dict."""
    names = sorted(n for n in os.listdir(folder)
                   if n.endswith(SIDECAR_SUFFIX))
    takes = []
    for n in names:
        path = os.path.join(folder, n)
        try:
            t = measure_take(path)
        except Exception as exc:
            takes.append({"clip": n.split(".")[0],
                          "error": "%s: %s" % (type(exc).__name__, exc)})
            continue
        if t is not None:
            takes.append(t)
    ratios = [t["seam_ratio"] for t in takes
              if isinstance(t, dict) and "seam_ratio" in t
              and t["seam_ratio"] == t["seam_ratio"]]   # filter NaN
    drifts = [t["composition_drift"] for t in takes
              if isinstance(t, dict) and "composition_drift" in t]
    max_ratio = max(ratios) if ratios else 0.0
    mean_drift = sum(drifts) / len(drifts) if drifts else 0.0
    return {
        "folder": folder,
        "take_count": len(takes),
        "takes": takes,
        "seam_ratio": round(max_ratio, 4),
        "verdict": verdict_for(max_ratio),
        "composition_drift": round(mean_drift, 4),
    }


def run(control_folder, test_folder):
    """Measure both arms and apply the acceptance criteria."""
    control = measure_folder(control_folder)
    test = measure_folder(test_folder)
    acc = protocol.acceptance(control, test)
    return {"control": control, "test": test, "acceptance": acc}


def _print_real_report(result):
    print("=" * 68)
    print("Phase 4 acceptance -- real takes")
    print("=" * 68)
    print()
    for arm_name in ("control", "test"):
        arm = result[arm_name]
        print("  %s (%s): %d takes, worst seam %.3f (%s), mean drift %.4f"
              % (arm_name, arm["folder"], arm["take_count"],
                 arm["seam_ratio"], arm["verdict"],
                 arm["composition_drift"]))
        for t in arm["takes"][:5]:                  # first 5 takes
            if "error" in t:
                print("    %-16s ERROR: %s" % (t["clip"], t["error"]))
                continue
            print("    %-16s %-9s ratio %6.3f %-10s drift %.4f"
                  % (t["clip"], t["relation"], t["seam_ratio"],
                     t["verdict"], t["composition_drift"]))
        if arm["take_count"] > 5:
            print("    ... (%d more)" % (arm["take_count"] - 5))
        print()
    a = result["acceptance"]
    print("  acceptance (control vs test): %s"
          % ("PASS" if a["accept"] else "FAIL"))
    for r in a["reasons"]:
        print("    - %s" % r)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--control", help="folder of control-arm takes")
    ap.add_argument("--test", help="folder of test-arm takes")
    ap.add_argument("--out", help="write the result JSON to this path")
    ap.add_argument("--synthetic", action="store_true",
                    help="run the synthetic baseline instead (no model, "
                    "no GPU, no folders)")
    args = ap.parse_args()
    if args.synthetic:
        result = run_synthetic()
        _print_report(result)
    elif args.control and args.test:
        result = run(args.control, args.test)
        _print_real_report(result)
    else:
        ap.error("either --synthetic or both --control and --test "
                 "are required")
    if args.out:
        with open(args.out, "w") as f:
            json.dump(result, f, indent=2)
        print()
        print("  result written to %s" % args.out)


if __name__ == "__main__":
    main()
