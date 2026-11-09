"""Magik Timeline (comfyui-magik-timeline): non-linear clip composition,
MiniMax H3 today.

Takes are clip pairs (MP4 + .mctx.safetensors latent sidecar) with
content-addressed lineage; continuation runs through the pins pipeline
(spec -> prepare -> apply) on core's native keyframe anchoring, and a
timeline widget composes the takes by smart-cut.

Ported from comfyui-obvpm-timeline v0.1.4 (GPL-3.0); provenance and the
full identity mapping live in docs/MAGIK_PORT.md. Node class names are
prefixed MAGIK_H3 so this pack and the upstream one can be installed
side by side (an explicit rollback scenario); the wire types, the
mctx_v1 sidecar format and the cond_v1 format are byte-identical to
upstream, so clips, sidecars and wires move between the two packs
freely. Saved upstream workflows need the one-time class-name remap
described in docs/MAGIK_PORT.md.

Companion to comfyui-obvpm (image loaders, gates, switches, bundles,
Value Presets), which keeps its own identity here unchanged.
"""

from .nodes_assemble import MAGIK_H3Assemble, MAGIK_H3Timeline
from .nodes_budget import MAGIK_H3VramHeadroom
from .nodes_encode import MAGIK_H3MCtxFromFrames
from .nodes_live import MAGIK_H3LivePreview
from .nodes_joint import (MAGIK_H3ContextWindows, MAGIK_H3JointAudioMask,
                          MAGIK_H3JointConditioning, MAGIK_H3JointLatent)
from .nodes_load import MAGIK_H3LoadMCtx, MAGIK_H3LoadVideoWithMCtx
from .nodes_render import MAGIK_H3JointRender
from .nodes_result import MAGIK_H3ResultPreview
from .nodes_pins import (
    MAGIK_H3MCtxApplyPins,
    MAGIK_H3MCtxApplyPinsStage2,
    MAGIK_H3MCtxPinSpec,
    MAGIK_H3TrimPinned,
)
from .nodes_save import (
    MAGIK_H3SaveMCtxForVideo,
    MAGIK_H3SaveVideoWithMCtx,
    MAGIK_H3TrimAndSaveVideoWithMCtx,
)

try:
    from . import preview_route
    preview_route.register()
except Exception:  # headless/test runs have no PromptServer; nodes still work
    import logging
    logging.getLogger("magik.h3").info(
        "magik.h3: preview route not registered (no server)", exc_info=True)

NODE_CLASS_MAPPINGS = {
    "MAGIK_H3SaveVideoWithMCtx": MAGIK_H3SaveVideoWithMCtx,
    "MAGIK_H3TrimAndSaveVideoWithMCtx": MAGIK_H3TrimAndSaveVideoWithMCtx,
    "MAGIK_H3SaveMCtxForVideo": MAGIK_H3SaveMCtxForVideo,
    "MAGIK_H3LoadVideoWithMCtx": MAGIK_H3LoadVideoWithMCtx,
    "MAGIK_H3LoadMCtx": MAGIK_H3LoadMCtx,
    "MAGIK_H3MCtxFromFrames": MAGIK_H3MCtxFromFrames,
    "MAGIK_H3MCtxPinSpec": MAGIK_H3MCtxPinSpec,
    "MAGIK_H3MCtxApplyPins": MAGIK_H3MCtxApplyPins,
    "MAGIK_H3MCtxApplyPinsStage2": MAGIK_H3MCtxApplyPinsStage2,
    "MAGIK_H3TrimPinned": MAGIK_H3TrimPinned,
    "MAGIK_H3JointLatent": MAGIK_H3JointLatent,
    "MAGIK_H3JointConditioning": MAGIK_H3JointConditioning,
    "MAGIK_H3JointAudioMask": MAGIK_H3JointAudioMask,
    "MAGIK_H3ContextWindows": MAGIK_H3ContextWindows,
    "MAGIK_H3VramHeadroom": MAGIK_H3VramHeadroom,
    "MAGIK_H3JointRender": MAGIK_H3JointRender,
    "MAGIK_H3Assemble": MAGIK_H3Assemble,
    "MAGIK_H3Timeline": MAGIK_H3Timeline,
    "MAGIK_H3ResultPreview": MAGIK_H3ResultPreview,
    "MAGIK_H3LivePreview": MAGIK_H3LivePreview,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "MAGIK_H3SaveVideoWithMCtx": "H3 MCtx Save Video",
    "MAGIK_H3TrimAndSaveVideoWithMCtx": "H3 MCtx Trim and Save Video",
    "MAGIK_H3SaveMCtxForVideo": "H3 MCtx Save",
    "MAGIK_H3LoadVideoWithMCtx": "H3 MCtx Load Video",
    "MAGIK_H3LoadMCtx": "H3 MCtx Load",
    "MAGIK_H3MCtxFromFrames": "H3 MCtx From Frames",
    "MAGIK_H3MCtxPinSpec": "H3 MCtx Pin Spec",
    "MAGIK_H3MCtxApplyPins": "H3 MCtx Apply Pins",
    "MAGIK_H3MCtxApplyPinsStage2": "H3 MCtx Apply Pins Stage 2",
    "MAGIK_H3TrimPinned": "H3 MCtx Trim Pinned",
    "MAGIK_H3JointLatent": "H3 Join Latents",
    "MAGIK_H3JointConditioning": "H3 Joint Conditioning",
    "MAGIK_H3JointAudioMask": "H3 Joint Audio Mask",
    "MAGIK_H3ContextWindows": "H3 Context Windowing",
    "MAGIK_H3VramHeadroom": "H3 VRAM Headroom",
    "MAGIK_H3JointRender": "H3 Joint VAE Decode and Save",
    "MAGIK_H3Assemble": "H3 MCtx Assemble",
    "MAGIK_H3Timeline": "H3 MCtx Timeline",
    "MAGIK_H3ResultPreview": "H3 MCtx Result Preview",
    "MAGIK_H3LivePreview": "H3 Live Preview",
}

WEB_DIRECTORY = "./web"

# Every node carries the pack's name, so a search for "magik" finds them
# all and a node in a workflow says where it came from. The parenthesised
# suffix mirrors upstream's "(obvpm)": with both packs installed the node
# menu shows which pack each entry came from.
_SUFFIX = "(magik)"
NODE_DISPLAY_NAME_MAPPINGS = {
    key: name if name.endswith(_SUFFIX) else "%s %s" % (name, _SUFFIX)
    for key, name in NODE_DISPLAY_NAME_MAPPINGS.items()
}
for _key in NODE_CLASS_MAPPINGS:
    NODE_DISPLAY_NAME_MAPPINGS.setdefault(_key, "%s %s" % (_key, _SUFFIX))

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
