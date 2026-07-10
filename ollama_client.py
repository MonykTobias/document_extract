"""Backward-compatible Ollama imports for older local tools."""

from pathlib import Path
import sys

SRC_ROOT = Path(__file__).resolve().parent / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from document_extract.llm.ollama import *  # noqa: F401,F403,E402
