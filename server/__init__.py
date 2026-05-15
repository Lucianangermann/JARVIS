"""JARVIS server package."""
import os

# macOS multi-libomp + fork safety. These have to be set BEFORE any C
# extension that uses OpenMP (numpy, torch, ctranslate2) is imported,
# which is why they live in server/__init__.py — Python loads this
# before server/main.py and before any of our modules.
#
# 1) KMP_DUPLICATE_LIB_OK=TRUE
#    Many ML wheels ship their own libomp.dylib. When two land in the
#    same process, OpenMP aborts with "Error #15: Initializing libiomp
#    .dylib, but found libiomp.dylib already initialized.". This tells
#    OpenMP to keep going. Officially "unsupported"; in practice fine
#    for inference. The clean fix is `pip uninstall openai-whisper`
#    once faster-whisper is in place.
#
# 2) KMP_INIT_AT_FORK=FALSE
#    Skip OMP runtime re-initialisation in forked children. Our
#    subprocess calls (osascript, say, open) do fork+exec — between the
#    fork and the exec the child briefly has parent OMP state, and
#    re-init then can deadlock the parent. FALSE keeps the child
#    clean.
#
# 3) OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
#    macOS Cocoa frameworks (used transitively by AppleScript /
#    Foundation) abort fork() from multithreaded processes by default.
#    Disabling lets osascript / say work while Whisper threads are
#    alive in the parent. Documented escape hatch from Apple's own
#    docs for exactly this scenario.
for _k, _v in (
    ("KMP_DUPLICATE_LIB_OK", "TRUE"),
    ("KMP_INIT_AT_FORK", "FALSE"),
    ("OBJC_DISABLE_INITIALIZE_FORK_SAFETY", "YES"),
):
    os.environ.setdefault(_k, _v)
