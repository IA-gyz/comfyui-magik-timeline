"""Run the upstream unittest suite:  python tests/run_all.py -v"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from stubs import add_comfyui_root  # noqa: E402

add_comfyui_root()
HERE = os.path.dirname(os.path.abspath(__file__))
suite = unittest.defaultTestLoader.discover(HERE, pattern="test_*.py")
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(0 if result.wasSuccessful() else 1)
