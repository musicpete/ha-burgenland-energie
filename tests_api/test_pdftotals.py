"""Invoice-total reader, tested on synthetic PDFs only (no real invoice data).

The builder mimics the structure of the portal's invoices: Flate content
streams, two-byte glyph codes that only make sense through a ToUnicode CMap,
TJ arrays with kerning between fragments, and a second decoy CMap.
"""

from __future__ import annotations

import zlib
from datetime import date

from custom_components.burgenland_energie.pdftotals import parse_totals, text_tokens

_ALPHABET = sorted(set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 .,:%+-()äöüÄÖÜß€"))
# Shuffled code assignment so that the raw bytes are not readable text.
_CODE = {ch: 0x0100 + (i * 37) % 251 for i, ch in enumerate(_ALPHABET)}


def _cmap(mapping: dict[str, int]) -> bytes:
    lines = [f"<{code:04X}> <{ord(ch):04X}>" for ch, code in mapping.items()]
    body = "\n".join(lines)
    return (
        "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
        "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
        f"{len(lines)} beginbfchar\n{body}\nendbfchar\nendcmap\n"
    ).encode()


def _encode(text: str) -> bytes:
    raw = b"".join(_CODE[ch].to_bytes(2, "big") for ch in text)
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _show(text: str) -> bytes:
    # split into kerned fragments like the real generator does
    parts = [text[i : i + 3] for i in range(0, len(text), 3)]
    arr = b"".join(b"(" + _encode(p) + b")-1" for p in parts)
    return b"BT\n/F1 -9 Tf\n0 0 Td\n[" + arr + b"]TJ\nET\n"


def _pdf(lines: list[str]) -> bytes:
    decoy = {ch: code + 1 for ch, code in _CODE.items()}
    content = b"q\n" + b"".join(_show(line) for line in lines) + b"Q\n"
    streams = [_cmap(decoy), content, _cmap(_CODE)]
    out = bytearray(b"%PDF-1.6\n")
    for n, data in enumerate(streams, start=1):
        comp = zlib.compress(data)
        out += f"{n} 0 obj\n<< /Length {len(comp)} /Filter /FlateDecode >>\nstream\n".encode()
        out += comp + b"\nendstream\nendobj\n"
    out += b"trailer\n<< >>\n%%EOF\n"
    return bytes(out)


LAYOUT_A = [
    "Rechnung",
    "Summe exkl. USt.",
    "1.000,00",
    "+20 % USt.",
    "200,00",
    "Kosten",
    "für",
    "Strom",
    "inkl. USt.",
    "1.200,00",
    "Ihre",
    "Gesamtkosten",
    "01.07.2000",
    "bis",
    "30.06.2001",
    "Teilbeträge",
    "inkl. USt.",
    "-1.100,00",
    "Nachzahlung",
    "100,00",
    "erster Teilbetrag inkl. USt.",
    "90,00",
    "Summe exkl. USt",
    "50,00",
]

LAYOUT_B = [
    "Ihre Gesamtkosten",
    "01.07.2001 bis 30.06.2002",
    "Strom",
    "2.400,00",
    "abzüglich bezahlte Teilbeträge",
    "Strom",
    "-2.500,00",
    "Guthaben",
    "-100,00",
    "zuzüglich erster Teilbetrag",
    "180,00",
]


def test_tokens_decode_through_cmap() -> None:
    tokens = text_tokens(_pdf(["Summe exkl. USt.", "Grüße (A)"]))
    assert tokens == ["Summe exkl. USt.", "Grüße (A)"]


def test_layout_with_net_and_vat() -> None:
    totals = parse_totals(_pdf(LAYOUT_A))
    assert totals is not None
    assert (totals.net, totals.vat, totals.gross) == (1000.0, 200.0, 1200.0)
    assert totals.paid == 1100.0
    assert totals.balance == 100.0
    assert totals.period_from == date(2000, 7, 1)
    assert totals.period_to == date(2001, 6, 30)


def test_layout_with_gross_only() -> None:
    totals = parse_totals(_pdf(LAYOUT_B))
    assert totals is not None
    assert totals.gross == 2400.0
    assert totals.net is None and totals.vat is None
    assert totals.paid == 2500.0
    assert totals.balance == -100.0
    assert (totals.period_from, totals.period_to) == (date(2001, 7, 1), date(2002, 6, 30))
    assert totals.as_dict()["brutto"] == 2400.0


def test_inconsistent_sum_is_rejected() -> None:
    lines = list(LAYOUT_A)
    lines[lines.index("1.200,00")] = "9.999,00"
    assert parse_totals(_pdf(lines)) is None


def test_no_summary_and_garbage() -> None:
    assert parse_totals(_pdf(["Mahnung", "12,00"])) is None
    assert parse_totals(b"%PDF-1.4\nnot really a pdf") is None
    assert parse_totals(b"") is None


def test_read_totals_caches_per_file(tmp_path) -> None:
    from custom_components.burgenland_energie.invoices import read_totals

    (tmp_path / "a.pdf").write_bytes(_pdf(LAYOUT_A))
    (tmp_path / "b.pdf").write_bytes(_pdf(["Mahnung"]))
    cache: dict = {}
    files = {"0001": "a.pdf", "0002": "b.pdf", "0003": "gone.pdf"}
    out = read_totals(tmp_path, files, cache)
    assert list(out) == ["0001"] and out["0001"].gross == 1200.0
    assert len(cache) == 2
    (tmp_path / "a.pdf").write_bytes(b"x")  # replaced file is parsed again
    assert read_totals(tmp_path, files, cache) == {}
