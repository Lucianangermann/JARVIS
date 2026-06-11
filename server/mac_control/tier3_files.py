"""Tier 3 — sandboxed file management.

Sandbox model
-------------
Every path is canonicalised with ``Path.expanduser().resolve()`` BEFORE
the allow/block check. That follows symlinks, so a "~/Desktop/escape"
symlink pointing at /etc/passwd resolves to /etc/passwd and is rejected.

iCloud-aware allow list
~~~~~~~~~~~~~~~~~~~~~~~
On a Mac with iCloud Drive enabled, ``~/Documents`` is itself a symlink
into ``~/Library/Mobile Documents/com~apple~CloudDocs/Documents``. We
add the *resolved* form of each allowed directory to the allow list at
startup, so iCloud-backed files in Documents/Desktop work transparently
even though their canonical path lives under ``~/Library``.

Block list takes precedence
~~~~~~~~~~~~~~~~~~~~~~~~~~~
``~/.ssh``, ``~/.aws``, ``~/Library/Keychains`` etc. are checked AFTER
allow, so even if a user symlinks one into Documents, the resolved path
hits the block check.

Deletion semantics
------------------
There is no permanent-delete action. ``trash()`` calls Finder's "delete"
verb via AppleScript, which moves to ~/.Trash. The user can restore from
there. The handlers never call ``os.remove`` / ``shutil.rmtree``.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from . import permission_manager
from .permission_manager import Tier

# --- sandbox configuration ------------------------------------------------- #

_ALLOWED_LITERAL = ("~/Desktop", "~/Downloads", "~/Documents")
_BLOCKED_LITERAL = (
    "~/.ssh", "~/.aws", "~/.gnupg", "~/.config",
    "~/Library/Keychains", "~/Library/Cookies",
    "~/Library/Application Support/com.apple.TCC",
    "/System", "/usr", "/etc", "/private/etc", "/private/var", "/Library",
)


def _expand_resolve(p: str) -> Path | None:
    """Resolve a path string symlink-aware. Returns None if it doesn't exist
    on disk (used for paths that may not be filesystem-real yet, like
    parent-only resolution)."""
    raw = Path(p).expanduser()
    try:
        return raw.resolve(strict=True)
    except (FileNotFoundError, OSError):
        return None


def _build_prefix_set(literals: tuple[str, ...]) -> tuple[Path, ...]:
    """Expand a list of ~-paths to both their literal and resolved forms."""
    seen: set[Path] = set()
    for lit in literals:
        raw = Path(lit).expanduser()
        seen.add(raw)
        real = _expand_resolve(lit)
        if real is not None:
            seen.add(real)
    return tuple(seen)


ALLOWED_DIRS: tuple[Path, ...] = _build_prefix_set(_ALLOWED_LITERAL)
BLOCKED_DIRS: tuple[Path, ...] = _build_prefix_set(_BLOCKED_LITERAL)

MAX_READ_BYTES = 100 * 1024
MAX_WRITE_BYTES = 100 * 1024
MAX_PDF_TEXT = 200 * 1024   # cap on extracted PDF text returned to Claude
MAX_PDF_PAGES = 80          # don't extract an unbounded number of pages


# --- helpers --------------------------------------------------------------- #

class SandboxError(ValueError):
    """The requested path is outside the sandbox."""


def _is_within(p: Path, base: Path) -> bool:
    try:
        p.relative_to(base)
        return True
    except ValueError:
        return False


def _validate_path(path: str, *, must_exist: bool) -> Path:
    """Canonicalise and sandbox-check a user-supplied path.

    must_exist=False is used for create operations: we resolve the parent
    (which must exist) and then append the leaf, so the resolution still
    follows any symlinks in the directory chain.
    """
    if not isinstance(path, str) or not path:
        raise SandboxError("Pfad fehlt.")
    raw = Path(path).expanduser()
    if must_exist:
        try:
            target = raw.resolve(strict=True)
        except FileNotFoundError as exc:
            raise SandboxError(f"Pfad existiert nicht: {raw}") from exc
    else:
        if not raw.parent.exists():
            raise SandboxError(f"Übergeordneter Ordner existiert nicht: {raw.parent}")
        target = raw.parent.resolve() / raw.name
    # Block check first — even an "allowed" prefix can't override a block.
    for base in BLOCKED_DIRS:
        if _is_within(target, base):
            raise SandboxError(f"Pfad ist in einer gesperrten Zone: {target}")
    # Allow check.
    if not any(_is_within(target, base) for base in ALLOWED_DIRS):
        raise SandboxError(
            f"Pfad liegt außerhalb der Sandbox: {target}\n"
            f"Erlaubt sind nur: {', '.join(str(d) for d in ALLOWED_DIRS)}"
        )
    return target


# --- handlers -------------------------------------------------------------- #

def _list_dir(*, path: str = "~/Desktop", **_kw) -> str:
    try:
        d = _validate_path(path, must_exist=True)
    except SandboxError as exc:
        return str(exc)
    if not d.is_dir():
        return f"Kein Ordner: {d}"
    items = []
    try:
        for child in sorted(d.iterdir()):
            kind = "📁" if child.is_dir() else "📄"
            size = "" if child.is_dir() else f"  {child.stat().st_size} B"
            items.append(f"{kind} {child.name}{size}")
    except PermissionError as exc:
        return f"Keine Leseberechtigung: {exc}"
    if not items:
        return f"{d} ist leer."
    return f"{d}:\n" + "\n".join(items[:200])  # cap at 200 entries


def _read_pdf(f: Path) -> str:
    """Extract text from a PDF (pypdf). Returns a clear note for scanned /
    image-only PDFs (no extractable text) rather than garbage."""
    try:
        from pypdf import PdfReader
    except Exception:  # noqa: BLE001
        return ("PDF-Lesen ist nicht verfügbar (pypdf nicht installiert: "
                "`pip install pypdf`).")
    try:
        reader = PdfReader(str(f))
        if getattr(reader, "is_encrypted", False):
            try:
                reader.decrypt("")  # try empty password
            except Exception:  # noqa: BLE001
                return "Die PDF ist passwortgeschützt — kann nicht gelesen werden."
        parts: list[str] = []
        for i, page in enumerate(reader.pages):
            if i >= MAX_PDF_PAGES:
                parts.append(f"…(weitere Seiten ab {MAX_PDF_PAGES} ausgelassen)")
                break
            t = (page.extract_text() or "").strip()
            if t:
                parts.append(t)
        text = "\n\n".join(parts).strip()
        if not text:
            return ("Die PDF enthält keinen extrahierbaren Text — sie ist "
                    "wahrscheinlich gescannt (Bild-PDF). Ich könnte sie per "
                    "Bilderkennung lesen, falls gewünscht.")
        if len(text) > MAX_PDF_TEXT:
            text = text[:MAX_PDF_TEXT] + "\n…(Text gekürzt)"
        return text
    except Exception as exc:  # noqa: BLE001
        return f"PDF-Lesen fehlgeschlagen: {exc}"


def _read_file(*, path: str = "", **_kw) -> str:
    try:
        f = _validate_path(path, must_exist=True)
    except SandboxError as exc:
        return str(exc)
    if not f.is_file():
        return f"Keine Datei: {f}"
    # PDFs: extract text rather than reading raw bytes as UTF-8.
    if f.suffix.lower() == ".pdf":
        return _read_pdf(f)
    if f.stat().st_size > MAX_READ_BYTES:
        return f"Datei zu groß ({f.stat().st_size} B, Limit {MAX_READ_BYTES})."
    try:
        raw = f.read_bytes()
    except OSError as exc:
        return f"Lesen fehlgeschlagen: {exc}"
    # Detect binary (NUL byte in the head) so we don't return garbage for
    # images/archives/office docs.
    if b"\x00" in raw[:8192]:
        return (f"Binärdatei ({f.suffix or 'ohne Endung'}) — kann nicht als "
                f"Text gelesen werden.")
    return raw.decode("utf-8", errors="replace")


def _create_file(*, path: str = "", content: str = "", **_kw) -> str:
    if not isinstance(content, str):
        return "content muss ein String sein."
    if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
        return f"Inhalt zu groß (Limit {MAX_WRITE_BYTES} B)."
    try:
        f = _validate_path(path, must_exist=False)
    except SandboxError as exc:
        return str(exc)
    if f.exists():
        return f"Datei existiert bereits: {f} — nicht überschrieben."
    try:
        f.write_text(content, encoding="utf-8")
    except OSError as exc:
        return f"Schreiben fehlgeschlagen: {exc}"
    return f"Datei erstellt: {f} ({len(content)} Zeichen)."


def _edit_file(*, path: str = "", content: str = "", mode: str = "overwrite",
               **_kw) -> str:
    """Edit an EXISTING text file: overwrite it, or append to it. (New files
    use create_file; PDFs can't be edited as text.)"""
    if not isinstance(content, str):
        return "content muss ein String sein."
    if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
        return f"Inhalt zu groß (Limit {MAX_WRITE_BYTES} B)."
    try:
        f = _validate_path(path, must_exist=True)
    except SandboxError as exc:
        return str(exc)
    if not f.is_file():
        return f"Keine Datei: {f}"
    if f.suffix.lower() == ".pdf":
        return "PDF-Dateien können nicht als Text bearbeitet werden."
    try:
        if mode == "append":
            with f.open("a", encoding="utf-8") as fh:
                fh.write(content)
            return f"An {f.name} angehängt ({len(content)} Zeichen)."
        # overwrite (default)
        f.write_text(content, encoding="utf-8")
        return f"{f.name} überschrieben ({len(content)} Zeichen)."
    except OSError as exc:
        return f"Bearbeiten fehlgeschlagen: {exc}"


def _create_dir(*, path: str = "", **_kw) -> str:
    try:
        d = _validate_path(path, must_exist=False)
    except SandboxError as exc:
        return str(exc)
    if d.exists():
        return f"Existiert bereits: {d}"
    try:
        d.mkdir(parents=False)
    except OSError as exc:
        return f"Anlegen fehlgeschlagen: {exc}"
    return f"Ordner erstellt: {d}"


def _rename(*, path: str = "", new_name: str = "", **_kw) -> str:
    if not isinstance(new_name, str) or not new_name:
        return "new_name fehlt."
    if "/" in new_name or new_name in (".", "..") or "\x00" in new_name:
        return "new_name darf keine Pfadtrenner oder Null-Bytes enthalten."
    try:
        src = _validate_path(path, must_exist=True)
    except SandboxError as exc:
        return str(exc)
    dst = src.parent / new_name
    try:
        dst = _validate_path(str(dst), must_exist=False)
    except SandboxError as exc:
        return str(exc)
    if dst.exists():
        return f"Ziel existiert bereits: {dst}"
    try:
        src.rename(dst)
    except OSError as exc:
        return f"Umbenennen fehlgeschlagen: {exc}"
    return f"Umbenannt: {src.name} → {dst.name}"


def _move(*, src: str = "", dst: str = "", **_kw) -> str:
    try:
        s = _validate_path(src, must_exist=True)
        d_raw = Path(dst).expanduser()
        # If dst is an existing directory, append source name
        if d_raw.exists() and d_raw.is_dir():
            d = _validate_path(str(d_raw / s.name), must_exist=False)
        else:
            d = _validate_path(dst, must_exist=False)
    except SandboxError as exc:
        return str(exc)
    if d.exists():
        return f"Ziel existiert bereits: {d}"
    try:
        shutil.move(str(s), str(d))
    except OSError as exc:
        return f"Verschieben fehlgeschlagen: {exc}"
    return f"Verschoben: {s} → {d}"


# Trash via Finder — moves to ~/.Trash, recoverable. We pass the resolved
# POSIX path via osascript argv, never as part of the script body.
_TR_TRASH = """
on run argv
    set thePath to POSIX file (item 1 of argv)
    tell application "Finder" to delete (thePath as alias)
end run
"""


def _trash(*, path: str = "", **_kw) -> str:
    try:
        p = _validate_path(path, must_exist=True)
    except SandboxError as exc:
        return str(exc)
    # Generous timeout: macOS shows a one-time "Allow JARVIS to control
    # Finder" prompt on the *first* Finder automation call after boot,
    # which can delay the script by 10+ seconds while the user clicks
    # OK. Subsequent calls are sub-second. See README_MAC_CONTROL.md.
    try:
        proc = subprocess.run(
            ["/usr/bin/osascript", "-", str(p)],
            input=_TR_TRASH,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ("Trash hat nicht geantwortet. Falls macOS einen "
                "Automation-Prompt zeigt, bitte 'OK' drücken und erneut versuchen.")
    except FileNotFoundError as exc:
        return f"Trash fehlgeschlagen: {exc}"
    if proc.returncode != 0:
        return f"Trash fehlgeschlagen: {proc.stderr.strip()}"
    return f"In den Papierkorb verschoben: {p.name}"


# --- registry -------------------------------------------------------------- #

_TIER3: tuple[tuple[str, callable, callable], ...] = (
    ("list_dir",    _list_dir,    lambda **p: f"Ordnerinhalt anzeigen: {p.get('path', '~/Desktop')}"),
    ("read_file",   _read_file,   lambda **p: f"Datei lesen: {p.get('path', '')}"),
    ("create_file", _create_file, lambda **p: f"Datei anlegen: {p.get('path', '')} ({len(p.get('content','') or '')} Z.)"),
    ("edit_file",   _edit_file,   lambda **p: f"Datei bearbeiten ({p.get('mode','overwrite')}): {p.get('path','')} ({len(p.get('content','') or '')} Z.)"),
    ("create_dir",  _create_dir,  lambda **p: f"Ordner anlegen: {p.get('path', '')}"),
    ("rename",      _rename,      lambda **p: f"Umbenennen: {p.get('path','')} → {p.get('new_name','')}"),
    ("move",        _move,        lambda **p: f"Verschieben: {p.get('src','')} → {p.get('dst','')}"),
    ("trash",       _trash,       lambda **p: f"In Papierkorb verschieben: {p.get('path','')}"),
)


def register_all() -> None:
    for name, handler, summary in _TIER3:
        permission_manager.register(name, Tier.FILES, handler, summary)


register_all()
