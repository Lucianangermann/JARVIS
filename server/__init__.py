"""JARVIS server package."""
import os

# macOS multi-libomp workaround.
# Many ML wheels ship their own copy of libomp.dylib (PyTorch from
# openai-whisper, CTranslate2 from faster-whisper, sometimes numpy).
# When two of them land in the same process, libomp aborts with
#     OMP: Error #15: Initializing libiomp5.dylib, but found libiomp5
#     .dylib already initialized.
# Setting KMP_DUPLICATE_LIB_OK=TRUE before any of those libs load tells
# OpenMP to keep going. Intel marks this "unsupported" because the two
# libs *could* be ABI-incompatible, but for our use (inference with
# stable models) it's safe in practice.
#
# The clean fix is to uninstall whichever Whisper backend you don't
# need (we prefer faster-whisper, so `pip uninstall openai-whisper`).
# We use setdefault so an operator override still wins.
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
