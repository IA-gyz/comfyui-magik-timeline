"""Shared test helpers (path bootstrap kept out of the pack modules)."""
import os
import sys


def add_comfyui_root():
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    CANDIDATES = [
        os.environ.get("COMFYUI_ROOT", ""),
        os.path.join(os.path.dirname(ROOT), "comfyui"),
        os.path.join(os.path.dirname(ROOT), "ComfyUI"),
        os.path.join(ROOT, "..", ".."),
    ]
    for cand in CANDIDATES:
        if cand and os.path.exists(os.path.join(cand, "folder_paths.py")):
            cand = os.path.abspath(cand)
            if cand not in sys.path:
                sys.path.insert(0, cand)
            break
    else:
        raise RuntimeError(
            "No ComfyUI checkout found for the test suite; set COMFYUI_ROOT.")
    # CPU-only hosts: comfy.cli_args parses sys.argv at import time and
    # comfy.model_management resolves its device from that at ITS import
    # time. When no CUDA build/device is available, force the --cpu path
    # before anything imports model_management. On a GPU machine this
    # leaves the parsed args untouched (upstream behaviour).
    try:
        import torch
        if not torch.cuda.is_available():
            import comfy.cli_args as _cli_args
            if getattr(_cli_args, "args", None) is None:
                _cli_args.args = _cli_args.parser.parse_args(["--cpu"])
            else:
                _cli_args.args.cpu = True
    except Exception:
        pass
