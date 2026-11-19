"""Load the external observer without adding its directory to sys.path.

Invoke this file through runpy.run_path from python -c in the unchanged checkout.
Every pytest argument is forwarded, including the fixed selector and lane flag.
The original pytest exit is the process exit. This script dispatches nothing.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

path = Path(__file__).with_name("ls15_queue_observer.py")
spec = importlib.util.spec_from_file_location("ls15_queue_observer", path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
raise SystemExit(pytest.main(sys.argv[1:], plugins=[module]))
