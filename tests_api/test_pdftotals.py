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


# A second font whose CMap maps the same codes to other characters: digits
# read through the wrong CMap still look like digits, so the choice must be
# made per font, not per text run.
_ALT = {ch: code for ch, code in zip(_ALPHABET, sorted(_CODE.values(), reverse=True))}
_FONTS = {b"F1": _CODE, b"F2": _ALT}


def _encode_with(text: str, table: dict[str, int]) -> bytes:
    raw = b"".join(table[ch].to_bytes(2, "big") for ch in text)
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _show(text: str, font: bytes = b"F1") -> bytes:
    # split into kerned fragments like the real generator does
    parts = [text[i : i + 3] for i in range(0, len(text), 3)]
    arr = b"".join(b"(" + _encode_with(p, _FONTS[font]) + b")-1" for p in parts)
    return b"BT\n/" + font + b" -9 Tf\n0 0 Td\n[" + arr + b"]TJ\nET\n"


def _pdf(lines: list[str], fonts: list[bytes] | None = None) -> bytes:
    fonts = fonts or [b"F1"] * len(lines)
    content = b"q\n" + b"".join(_show(line, f) for line, f in zip(lines, fonts)) + b"Q\n"
    streams = [_cmap(_ALT), content, _cmap(_CODE)]
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
    "Mahnkosten",
    "10,00",
    "Ihre",
    "Gesamtkosten",
    "01.07.2000",
    "bis",
    "30.06.2001",
    "inkl. USt.",
    "1.210,00",
    "Stromkostenzuschuss",
    "0 % USt.",
    "-110,00",
    "Teilbeträge",
    "inkl. USt.",
    "-1.000,00",
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
    "inkl. USt.",
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


def test_tokens_use_one_cmap_per_font() -> None:
    lines = ["Summe exkl. USt.", "1.000,00", "Gesamtkosten", "389,32"]
    tokens = text_tokens(_pdf(lines, [b"F1", b"F2", b"F2", b"F1"]))
    assert tokens == lines


def test_settlement_with_fees_and_subsidy() -> None:
    totals = parse_totals(_pdf(LAYOUT_A))
    assert totals is not None
    assert (totals.energy_net, totals.energy_vat, totals.energy_gross) == (1000.0, 200.0, 1200.0)
    assert totals.total == 1210.0
    assert totals.other == 10.0
    assert totals.subsidy == 110.0
    assert totals.effective == 1100.0
    assert totals.paid == 1000.0
    assert totals.balance == 100.0
    assert totals.period_from == date(2000, 7, 1)
    assert totals.period_to == date(2001, 6, 30)


def test_layout_with_gross_only() -> None:
    totals = parse_totals(_pdf(LAYOUT_B))
    assert totals is not None
    assert totals.total == 2400.0
    assert totals.energy_net is None and totals.other is None
    assert totals.paid == 2500.0
    assert totals.balance == -100.0
    assert (totals.period_from, totals.period_to) == (date(2001, 7, 1), date(2002, 6, 30))
    assert totals.as_dict()["brutto"] == 2400.0


def test_inconsistent_electricity_block_is_ignored() -> None:
    lines = list(LAYOUT_A)
    lines[lines.index("1.200,00")] = "9.999,00"
    totals = parse_totals(_pdf(lines))
    assert totals is not None and totals.total == 1210.0 and totals.energy_gross is None


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
    assert list(out) == ["0001"] and out["0001"].total == 1210.0
    assert len(cache) == 2
    (tmp_path / "a.pdf").write_bytes(b"x")  # replaced file is parsed again
    assert read_totals(tmp_path, files, cache) == {}
