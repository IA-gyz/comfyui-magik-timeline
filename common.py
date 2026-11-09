# Magik Timeline -- ported from comfyui-obvpm-timeline v0.1.4
# (GPL-3.0). Provenance and identity mapping: docs/MAGIK_PORT.md
"""Pieces shared by the node modules."""


class AnyType(str):
    """A socket type that matches every other type (ComfyUI's "*" idiom)."""

    def __ne__(self, other):
        return False


ANY = AnyType("*")
