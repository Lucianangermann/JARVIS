"""Tests for file read (incl. PDF) + edit in mac_control/tier3_files.py."""
from __future__ import annotations

from pathlib import Path

import pytest

import server.mac_control.tier3_files as t3


def _make_pdf(text: str) -> bytes:
    """A correctly-structured minimal one-page text PDF (valid xref)."""
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>",
    ]
    stream = b"BT /F1 24 Tf 72 700 Td (" + text.encode() + b") Tj ET"
    objs.append(b"<</Length " + str(len(stream)).encode() + b">>stream\n"
                + stream + b"\nendstream")
    objs.append(b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>")
    pdf = b"%PDF-1.4\n"
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(pdf))
        pdf += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xo = len(pdf)
    pdf += b"xref\n0 " + str(len(objs) + 1).encode() + b"\n0000000000 65535 f \n"
    for off in offs:
        pdf += ("%010d 00000 n \n" % off).encode()
    pdf += (b"trailer<</Size " + str(len(objs) + 1).encode()
            + b"/Root 1 0 R>>\nstartxref\n" + str(xo).encode() + b"\n%%EOF")
    return pdf


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """Point the path validator at a temp dir so we can exercise the
    handlers without touching the real ~/Documents."""
    monkeypatch.setattr(t3, "_validate_path",
                        lambda path, must_exist=False: Path(path))
    return tmp_path


def test_read_pdf_extracts_text(sandbox) -> None:
    pdf = sandbox / "M4.pdf"
    pdf.write_bytes(_make_pdf("Hallo Mechatronik M4 Inhalt"))
    out = t3._read_file(path=str(pdf))
    assert "Mechatronik" in out and "M4" in out
    # Full read announces the page count so Claude can request pages.
    assert "Seite" in out


def test_read_pdf_single_page(sandbox) -> None:
    pdf = sandbox / "doc.pdf"
    pdf.write_bytes(_make_pdf("Seiteninhalt eins"))
    # Page 1 returns just that page with a header.
    out = t3._read_file(path=str(pdf), page=1)
    assert "Seite 1" in out and "Seiteninhalt" in out
    # Out-of-range page → clear error, not a crash.
    oob = t3._read_file(path=str(pdf), page=99)
    assert "existiert nicht" in oob


def test_scanned_pdf_note(sandbox) -> None:
    # A PDF with no extractable text → a clear note, not garbage.
    pdf = sandbox / "scan.pdf"
    pdf.write_bytes(_make_pdf(""))  # empty text object
    out = t3._read_file(path=str(pdf))
    assert "kein" in out.lower() or "gescannt" in out.lower()


def test_read_text_file(sandbox) -> None:
    f = sandbox / "notiz.txt"
    f.write_text("Zeile 1\nZeile 2", encoding="utf-8")
    assert t3._read_file(path=str(f)) == "Zeile 1\nZeile 2"


def test_binary_file_not_read_as_text(sandbox) -> None:
    f = sandbox / "bild.bin"
    f.write_bytes(b"\x00\x01\x02PK\x03")
    assert "Binär" in t3._read_file(path=str(f))


def test_edit_overwrite_and_append(sandbox) -> None:
    f = sandbox / "doc.txt"
    f.write_text("alt", encoding="utf-8")
    assert "überschrieben" in t3._edit_file(path=str(f), content="neu")
    assert f.read_text() == "neu"
    assert "angehängt" in t3._edit_file(path=str(f), content=" +x", mode="append")
    assert f.read_text() == "neu +x"


def test_edit_requires_existing_file(sandbox) -> None:
    out = t3._edit_file(path=str(sandbox / "nope.txt"), content="x")
    assert "Keine Datei" in out


def test_edit_rejects_pdf(sandbox) -> None:
    pdf = sandbox / "x.pdf"
    pdf.write_bytes(_make_pdf("hi"))
    assert "PDF" in t3._edit_file(path=str(pdf), content="x")


def test_edit_file_registered_tier3() -> None:
    names = [n for n, _, _ in t3._TIER3]
    assert "edit_file" in names and "read_file" in names
