# Magik Timeline -- one-time workflow migration (upstream 0.1.4 -> Magik)
# (GPL-3.0). Provenance and identity mapping: docs/MAGIK_PORT.md secs. 1.1, 3.
"""Remap a saved upstream workflow's node class names to Magik.

A workflow JSON saved against upstream comfyui-obvpm-timeline 0.1.4
stores the OLD class names in `nodes[].type` (and inside group-node /
subgraph definitions). This pack's 19 node classes carry the `MAGIK_`
prefix so both packs can be installed side by side; a workflow saved
against upstream therefore has to be remapped once to run here.

WHAT IS REWRITTEN
    Any JSON string stored under a `type` or `class_type` key that is
    EXACTLY one of the 19 upstream class names becomes the
    corresponding `MAGIK_` name. `class_type` covers the API/prompt
    format; `type` covers the litegraph (UI) format, and a recursive
    walk reaches nested group-node / subgraph definitions whatever key
    they ride under.

WHAT IS LEFT ALONE
    Everything else: links, widget values, wire types (`OBVPM_H3_*`,
    `OBVPM_BUNDLE`), companion nodes (`MinimaxH3LatentUpscaler3D`,
    `SpectrumApplyMiniMaxH3`, ...), group titles (`"H3 Joint Refine
    Sampling"`) and every other string. The port doc's §3 shows a
    substring replace of the 19 names would also be safe ("no
    Magik-or-upstream widget value or property key appears among
    them"), but this walk is stricter: only exact type/class_type
    values move, so a note that happens to say `H3Timeline` in prose
    stays prose.

THE SCRIPT IS IDEMPOTENT. A Magik workflow (or a file already migrated)
comes out with zero changes; running it twice is a no-op.

Rollback is symmetrical: uninstall Magik, reinstall upstream 0.1.4,
remap the class names back -- pass --reverse for that direction.
Sidecars, takes and wires written by either pack are readable by the
other; nothing on disk besides the workflow JSON moves.

USAGE
    # in place (a .bak copy is written next to each file first)
    python migrate_workflows.py workflow.json more.json

    # whole folders (top-level *.json)
    python migrate_workflows.py saved_workflows/

    # write remapped copies to another folder, originals untouched
    python migrate_workflows.py --out remapped/ saved_workflows/

    # no .bak backups (you have your own)
    python migrate_workflows.py --no-backup workflow.json

    # back the other way (Magik -> upstream, for rollback)
    python migrate_workflows.py --reverse workflow.json

Exit status 0 when every file was processed; 1 if any file could not
be read or parsed (the other files are still processed).
"""
import argparse
import json
import os
import shutil
import sys

# The 19 upstream class names, in the order docs/MAGIK_PORT.md sec. 1.1
# lists them. Deliberately hardcoded: this script runs standalone on a
# machine without ComfyUI (stdlib only), and the list is frozen by the
# port contract -- Magik 0.2.0 renamed exactly these 19 and nothing
# else, so a dynamic import of NODE_CLASS_MAPPINGS would add a
# dependency without adding correctness.
UPSTREAM_CLASSES = [
    "H3Assemble",
    "H3ContextWindows",
    "H3JointAudioMask",
    "H3JointConditioning",
    "H3JointLatent",
    "H3JointRender",
    "H3LivePreview",
    "H3LoadMCtx",
    "H3LoadVideoWithMCtx",
    "H3MCtxApplyPins",
    "H3MCtxFromFrames",
    "H3MCtxPinSpec",
    "H3ResultPreview",
    "H3SaveMCtxForVideo",
    "H3SaveVideoWithMCtx",
    "H3Timeline",
    "H3TrimAndSaveVideoWithMCtx",
    "H3TrimPinned",
    "H3VramHeadroom",
]

# direction -> {old: new}. TYPE_KEYS are the only JSON keys whose exact
# string values are considered a class name.
TO_MAGIK = {name: "MAGIK_" + name for name in UPSTREAM_CLASSES}
TO_UPSTREAM = {"MAGIK_" + name: name for name in UPSTREAM_CLASSES}
TYPE_KEYS = ("type", "class_type")


def remap(obj, mapping):
    """Recursively remap class names in `obj` (dict/list tree).

    Returns (new_obj, count). Strings are left alone unless they are
    the exact value of a `type` / `class_type` key.
    """
    count = 0
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in TYPE_KEYS and isinstance(v, str) and v in mapping:
                out[k] = mapping[v]
                count += 1
            else:
                nv, n = remap(v, mapping)
                out[k] = nv
                count += n
        return out, count
    if isinstance(obj, list):
        out = []
        for v in obj:
            nv, n = remap(v, mapping)
            out.append(nv)
            count += n
        return out, count
    return obj, 0


def collect_json_files(paths):
    """Files named by the user plus *.json at the top level of named
    folders (no recursion: a ComfyUI workflow folder is flat, and
    recursing could surprise)."""
    files = []
    for p in paths:
        if os.path.isfile(p):
            files.append(p)
        elif os.path.isdir(p):
            for n in sorted(os.listdir(p)):
                if n.lower().endswith(".json"):
                    cand = os.path.join(p, n)
                    if os.path.isfile(cand):
                        files.append(cand)
        else:
            print("  skip %s: not a file or folder" % p)
    return files


def migrate_file(path, mapping, out_dir=None, backup=True,
                 pack_name="Magik"):
    """Remap one file. Returns (status, remapped_count, message)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        doc = json.loads(raw)
    except Exception as exc:
        return ("error", 0, "unreadable (%s: %s)" % (type(exc).__name__, exc))

    new_doc, count = remap(doc, mapping)

    if out_dir is not None:
        dest = os.path.join(out_dir, os.path.basename(path))
        os.makedirs(out_dir, exist_ok=True)
        with open(dest, "w", encoding="utf-8") as f:
            json.dump(new_doc, f, indent=2, ensure_ascii=False)
            f.write("\n")
        return ("written", count, "-> %s" % dest)

    if count == 0:
        # leave an already-migrated or unrelated file byte-identical,
        # and do not clutter its folder with a .bak either
        return ("clean", 0, "no %s class names -- left as is" % pack_name)
    if backup:
        bak = path + ".bak"
        if not os.path.exists(bak):
            shutil.copy2(path, bak)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(new_doc, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return ("remapped", count, "%d class name%s"
            % (count, "" if count == 1 else "s"))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="The 19 class names and the full mapping are in "
               "docs/MAGIK_PORT.md secs. 1.1 and 3.")
    ap.add_argument("paths", nargs="*", help="workflow .json files and/or "
                    "folders whose top-level *.json files are migrated")
    ap.add_argument("--out", metavar="DIR", default=None,
                    help="write remapped copies to DIR instead of editing "
                    "in place; originals are untouched and no .bak is made")
    ap.add_argument("--no-backup", action="store_true",
                    help="in-place mode without the .bak copy")
    ap.add_argument("--reverse", action="store_true",
                    help="remap Magik class names BACK to upstream "
                    "(the rollback direction)")
    args = ap.parse_args(argv)

    if not args.paths:
        ap.error("give at least one workflow .json file or folder")

    mapping = TO_UPSTREAM if args.reverse else TO_MAGIK
    pack_name = "Magik" if args.reverse else "upstream 0.1.4"
    direction = ("Magik -> upstream" if args.reverse
                 else "upstream 0.1.4 -> Magik")
    print("migrating %s: %d class names" % (direction, len(mapping)))

    files = collect_json_files(args.paths)
    if not files:
        print("no .json files found in: %s" % ", ".join(args.paths))
        return 1

    statuses = {}
    for path in files:
        status, count, msg = migrate_file(path, mapping,
                                          out_dir=args.out,
                                          backup=not args.no_backup,
                                          pack_name=pack_name)
        statuses[status] = statuses.get(status, 0) + 1
        print("  %-52s %-9s %s" % (path, status, msg))

    summary = "  ".join("%s: %d" % (k, v) for k, v in sorted(statuses.items()))
    print("%d file%s  %s" % (len(files), "" if len(files) == 1 else "s",
                             summary))
    return 0 if statuses.get("error", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
