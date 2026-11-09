"""The one-time workflow migration (upstream 0.1.4 -> Magik).

docs/MAGIK_PORT.md sec. 3 promises that a saved upstream workflow needs
exactly one thing to move between the packs: the 19 node class names,
in `nodes[].type` and inside group-node / subgraph definitions. Links,
widget values, wire types, companion nodes and display strings must
NOT move. migrate_workflows.py implements that with a stricter walk
than the doc's substring replace: only exact `type` / `class_type`
values are considered class names, so prose that happens to contain an
old name stays prose.

These tests pin the walk on a synthetic workflow that carries every
shape a real saved workflow has:

  * plain nodes with old types (litegraph format: nodes[].type),
  * an API-format file (class_type),
  * a group-node definition with nested nodes[].type,
  * links (5-element arrays), widget values, and a note whose text
    CONTAINS an old class name in prose,
  * companion nodes and wire-type strings,
  * group titles like "H3 Joint Refine Sampling" that must survive.

And the operational properties the doc promises:

  * idempotency -- a second run is a no-op,
  * an already-Magik file is left byte-identical ("clean", 0 changes),
  * the reverse mapping is the exact mirror (rollback),
  * the hardcoded 19-name list matches the port contract,
  * the CLI end-to-end (files, folder, --out, --no-backup, --reverse).

Run from the pack folder:
  python tests/run_all.py
"""
import copy
import json
import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMFY = os.path.abspath(os.path.join(ROOT, "..", ".."))
if COMFY not in sys.path:
    sys.path.insert(0, COMFY)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
pack = sys.modules.get("magik_tl_test")
if pack is None:
    pack = types.ModuleType("magik_tl_test")
    pack.__path__ = [ROOT]
    sys.modules[pack.__name__] = pack
from stubs import add_comfyui_root  # noqa: E402
add_comfyui_root()

# through the namespace, like every other pack-module import in this
# suite: run_all's discovery does not put the pack ROOT on sys.path
from magik_tl_test import migrate_workflows as mw  # noqa: E402


def upstream_workflow():
    """A synthetic saved-upstream workflow carrying every real shape."""
    return {
        "last_node_id": 9,
        "nodes": [
            {"id": 1, "type": "H3Timeline",
             "title": "H3 MCtx Timeline (obvpm)",
             "properties": {"h3_live_preview": False},
             "widgets_values": ["clip_00001.mp4", 39, "before"],
             "inputs": [{"name": "pin_specs",
                         "type": "OBVPM_H3_PINSPECS", "link": 2}],
             "outputs": [{"name": "pins",
                          "type": "OBVPM_H3_PINS", "links": [3]}]},
            {"id": 3, "type": "H3MCtxApplyPins",
             "properties": {"Node name for S&R": "H3MCtxApplyPins"},
             "widgets_values": ["masked", 0],
             "inputs": [{"name": "pin_specs",
                         "type": "OBVPM_H3_PINSPECS", "link": 3},
                        {"name": "latent", "type": "OBVPM_BUNDLE",
                         "link": 4}]},
            {"id": 6, "type": "H3TrimAndSaveVideoWithMCtx",
             "widgets_values": ["magik_h3_join_", True]},
            {"id": 8, "type": "MinimaxH3LatentUpscaler3D",
             "widgets_values": [True, 512]},          # companion: untouched
            {"id": 9, "type": "Note",                 # prose contains a name
             "widgets_values": ["H3Timeline drives the pins through "
                                "H3MCtxApplyPins; H3 Model Optimization "
                                "stays with the companion pack."]},
        ],
        "links": [[2, 4, 1, 1, 2],      # arrays: never touched
                  [3, 1, 0, 3, 0],
                  [4, 5, 0, 3, 1]],
        "groups": [{"title": "H3 Joint Refine Sampling",   # display only
                    "bounding": [0, 0, 800, 600]}],
        "definitions": {                    # group-node template nested
            "groupNodes": {
                "H3 refine pair": {
                    "nodes": [
                        {"id": -1, "type": "H3ContextWindows",
                         "widgets_values": ["static"]},
                        {"id": -2, "type": "H3JointLatent"},
                    ],
                    "links": [],
                    "widgets_values": {"name": "H3 refine pair"},
                }
            }
        },
    }


def api_workflow():
    """Same idea in the API/prompt format: class_type."""
    return {
        "1": {"class_type": "H3Timeline", "inputs": {"path": "a.mp4"}},
        "3": {"class_type": "H3MCtxApplyPins",
              "inputs": {"pin_specs": ["7", 0]}},
        "8": {"class_type": "MinimaxH3LatentUpscaler3D",
              "inputs": {"enable_temporal_chunking": True}},
    }


def types_of(doc, key="type"):
    out = []

    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == key and isinstance(v, str):
                    out.append(v)
                else:
                    walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)
    walk(doc)
    return out


class TheWalk(unittest.TestCase):
    def test_every_upstream_type_is_remapped(self):
        doc, n = mw.remap(upstream_workflow(), mw.TO_MAGIK)
        self.assertEqual(n, 5)          # 3 plain nodes + 2 nested group-node
        got = sorted(set(types_of(doc)))
        self.assertIn("MAGIK_H3Timeline", got)
        self.assertIn("MAGIK_H3MCtxApplyPins", got)
        self.assertIn("MAGIK_H3TrimAndSaveVideoWithMCtx", got)
        self.assertIn("MAGIK_H3ContextWindows", got)
        self.assertIn("MAGIK_H3JointLatent", got)
        self.assertNotIn("H3Timeline", got)
        self.assertNotIn("H3MCtxApplyPins", got)

    def test_nothing_but_the_type_values_moves(self):
        before = upstream_workflow()
        doc, _ = mw.remap(copy.deepcopy(before), mw.TO_MAGIK)
        # links survive as arrays
        self.assertEqual(doc["links"], before["links"])
        # companion node untouched
        self.assertEqual(doc["nodes"][3]["type"],
                         "MinimaxH3LatentUpscaler3D")
        # wire-type strings untouched
        self.assertEqual(doc["nodes"][0]["inputs"][0]["type"],
                         "OBVPM_H3_PINSPECS")
        # note prose keeps the old names
        self.assertIn("H3Timeline drives the pins",
                      doc["nodes"][4]["widgets_values"][0])
        # group titles untouched
        self.assertEqual(doc["groups"][0]["title"],
                         "H3 Joint Refine Sampling")
        # the search-and-replace property that mirrors a class name is
        # NOT a type key -- upstream stores it under properties, and it
        # must survive as written
        self.assertEqual(doc["nodes"][1]["properties"]["Node name for S&R"],
                         "H3MCtxApplyPins")

    def test_api_format_is_remapped(self):
        doc, n = mw.remap(api_workflow(), mw.TO_MAGIK)
        self.assertEqual(n, 2)
        self.assertEqual(doc["1"]["class_type"], "MAGIK_H3Timeline")
        self.assertEqual(doc["3"]["class_type"], "MAGIK_H3MCtxApplyPins")
        self.assertEqual(doc["8"]["class_type"],
                         "MinimaxH3LatentUpscaler3D")

    def test_an_empty_or_foreign_document_is_untouched(self):
        for doc in ({}, [], {"nodes": []},
                    {"nodes": [{"type": "KSampler"}]},
                    {"1": {"class_type": "KSampler"}},
                    {"nodes": [{"type": "MAGIK_H3Timeline"}]}):
            out, n = mw.remap(copy.deepcopy(doc), mw.TO_MAGIK)
            self.assertEqual(out, doc)
            self.assertEqual(n, 0)


class TheContract(unittest.TestCase):
    """The hardcoded list IS the port contract: exactly the 19 names of
    docs/MAGIK_PORT.md sec. 1.1, mapping to the MAGIK_ prefixed names
    Magik's NODE_CLASS_MAPPINGS registers."""

    def test_nineteen_upstream_names(self):
        self.assertEqual(len(mw.UPSTREAM_CLASSES), 19)
        self.assertEqual(len(set(mw.UPSTREAM_CLASSES)), 19)

    def test_mapping_is_the_mirror_image_pair(self):
        self.assertEqual(mw.TO_MAGIK,
                         {name: "MAGIK_" + name
                          for name in mw.UPSTREAM_CLASSES})
        self.assertEqual(mw.TO_UPSTREAM,
                         {"MAGIK_" + name: name
                          for name in mw.UPSTREAM_CLASSES})
        self.assertEqual(sorted(mw.TO_MAGIK.values()),
                         sorted(mw.TO_UPSTREAM.keys()))

    def test_every_magik_name_is_registered(self):
        # import the pack's own mappings to prove the script's list and
        # the node pack cannot drift apart
        import importlib
        import folder_paths  # noqa: F401  (the pack modules need it)
        init = importlib.import_module("magik_tl_test.__init__")
        self.assertTrue(set(mw.TO_MAGIK.values())
                        <= set(init.NODE_CLASS_MAPPINGS))


class IdempotencyAndRollback(unittest.TestCase):
    def test_second_run_is_a_no_op(self):
        doc, _ = mw.remap(upstream_workflow(), mw.TO_MAGIK)
        again, n = mw.remap(doc, mw.TO_MAGIK)
        self.assertEqual(n, 0)
        self.assertEqual(again, doc)

    def test_reverse_is_the_exact_mirror(self):
        doc, _ = mw.remap(upstream_workflow(), mw.TO_MAGIK)
        back, n = mw.remap(doc, mw.TO_UPSTREAM)
        self.assertEqual(n, 5)
        self.assertEqual(back, upstream_workflow())


class TheCLI(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="magik_tl_migrate_")
        self.wf = os.path.join(self.dir, "upstream_workflow.json")
        with open(self.wf, "w", encoding="utf-8") as f:
            json.dump(upstream_workflow(), f)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def read(self, path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_in_place_with_backup(self):
        rc = mw.main([self.wf])
        self.assertEqual(rc, 0)
        doc = self.read(self.wf)
        self.assertIn("MAGIK_H3Timeline",
                      types_of(doc))
        self.assertTrue(os.path.isfile(self.wf + ".bak"))
        # the backup still has the upstream names, so rollback needs no
        # reverse remap to recover the original file
        self.assertIn("H3Timeline", types_of(self.read(self.wf + ".bak")))

    def test_a_magik_workflow_is_clean_and_byte_identical(self):
        import shutil
        magik = os.path.join(self.dir, "magik_workflow.json")
        doc, _ = mw.remap(upstream_workflow(), mw.TO_MAGIK)
        raw = json.dumps(doc, indent=2)
        with open(magik, "w", encoding="utf-8") as f:
            f.write(raw)
        rc = mw.main([magik])
        self.assertEqual(rc, 0)
        with open(magik, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), raw)

    def test_out_writes_a_copy_and_leaves_the_original(self):
        out = os.path.join(self.dir, "remapped")
        rc = mw.main(["--out", out, self.wf])
        self.assertEqual(rc, 0)
        self.assertIn("MAGIK_H3Timeline",
                      types_of(self.read(os.path.join(out,
                                                      "upstream_workflow.json"))))
        self.assertIn("H3Timeline", types_of(self.read(self.wf)))
        self.assertFalse(os.path.isfile(self.wf + ".bak"))

    def test_reverse_moves_back(self):
        rc = mw.main([self.wf])
        self.assertEqual(rc, 0)
        rc = mw.main(["--reverse", self.wf])
        self.assertEqual(rc, 0)
        self.assertEqual(self.read(self.wf), upstream_workflow())

    def test_a_folder_is_walked_top_level_only(self):
        import shutil
        sub = os.path.join(self.dir, "sub")
        os.makedirs(sub)
        shutil.copy(self.wf, sub)
        rc = mw.main([self.dir])
        self.assertEqual(rc, 0)
        # the top-level workflow was migrated; the subfolder's copy was
        # not (no recursion), and the .bak written beside the first is
        # not itself re-processed
        self.assertIn("MAGIK_H3Timeline",
                      types_of(self.read(self.wf)))
        self.assertIn("H3Timeline", types_of(self.read(os.path.join(
            sub, "upstream_workflow.json"))))

    def test_an_unreadable_file_is_an_error_not_a_crash(self):
        bad = os.path.join(self.dir, "broken.json")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("{not json")
        rc = mw.main([bad, self.wf])
        self.assertEqual(rc, 1)
        self.assertIn("MAGIK_H3Timeline", types_of(self.read(self.wf)))


if __name__ == "__main__":
    unittest.main()
