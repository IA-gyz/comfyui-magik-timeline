"""pytest bootstrap: put a ComfyUI checkout on sys.path.

The upstream suite assumes the pack lives in ComfyUI/custom_nodes (the tests
insert ROOT/../.. and import folder_paths from there). When running from a
standalone checkout, set COMFYUI_ROOT or keep a ComfyUI clone next to this
repository (../comfyui or ../ComfyUI).

NOTE (pytest 9 + pack root __init__.py): with the pack's __init__.py at the
repo root, pytest's package collection imports the root as a top-level module
named "__init__" during item setup, which breaks on relative imports. Run the
suite with `python tests/run_all.py` (unittest discovery, the upstream
runner) instead of pytest.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CANDIDATES = [
    os.environ.get("COMFYUI_ROOT", ""),
    os.path.join(os.path.dirname(ROOT), "comfyui"),
    os.path.join(os.path.dirname(ROOT), "ComfyUI"),
    os.path.join(ROOT, "..", ".."),
]
for cand in CANDIDATES:
    if cand and os.path.exists(os.path.join(cand, "folder_paths.py")):
        if cand not in sys.path:
            sys.path.insert(0, os.path.abspath(cand))
        break
else:
    raise RuntimeError(
        "No ComfyUI checkout found for the test suite; set COMFYUI_ROOT.")


# CPU-only hosts: comfy.model_management resolves its device at import
# time from cli_args; make it take the --cpu path before anything imports
# it. On a GPU machine this changes nothing.
try:
    import comfy.cli_args as _cli_args
    if getattr(_cli_args, "args", None) is None:
        _cli_args.args = _cli_args.parser.parse_args(["--cpu"])
except Exception:
    pass
