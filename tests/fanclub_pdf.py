"""Builds synthetic energy community invoices for the tests.

Only placeholder values: no real names, addresses, numbers or bank details.
"""

from __future__ import annotations

import zlib


def _escape(text: str) -> bytes:
    raw = text.encode("latin-1")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def make_pdf(tokens: list[str]) -> bytes:
    """A small PDF whose only content stream draws the given text runs in order."""
    body = b"BT /F1 9 Tf " + b" ".join(b"(" + _escape(t) + b") Tj" for t in tokens) + b" ET"
    stream = zlib.compress(body)
    pdf = (
        b"%PDF-1.4\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 2 0 R /Contents 4 0 R >> endobj\n"
        b"4 0 obj << /Length " + str(len(stream)).encode() + b" /Filter /FlateDecode >>\nstream\n"
        + stream
        + b"\nendstream endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"
    )
    return pdf + b"%" + b"0" * 1200 + b"\n"  # padded above the minimum file size


def invoice_tokens(
    nr: str,
    pod: str,
    period: tuple[str, str],
    positions: list[tuple[str, str, str, str, str, str]],
    net: str,
    vat: str,
    gross: str,
    issued: str = "15.04.2030",
    old_layout: bool = False,
) -> list[str]:
    """positions: (pos, text, price, qty, unit, value_and_months 'value|MM/YYYY[ - MM/YYYY]')."""
    toks = [
        "Ihre Rechnungsdaten:", "Kundennummer", "00.000.001", "Rechnungsnummer", nr,
        "Rechnungsdatum", issued, "Vertragskonto", "00000000001",
        "Max Muster", "Musterweg 1", "0000 Musterort",  # placeholder recipient
        "Rechnung", "Zählpunkt:", "AT0000000000000000000000000000001",
        f"Zählpunkt Name: {pod}", "Belieferungsstart:", "01.01.2029",
        "Rechnungszeitraum:", period[0], "bis", period[1],
        "Pos.", "Bezeichnung", "Einzelpreis", "Menge", "Einheit", "Wert",
    ]
    for pos, text, price, qty, unit, rest in positions:
        value, months = rest.split("|")
        if old_layout:
            toks += [pos, text, price, qty, value, unit, f"Zeitraum: {months}"]
        else:
            toks += [pos, text, price, qty, unit, value, f"Zeitraum: {months}"]
    toks += [
        "Summe exkl. USt.", net, "+ 20 % USt.", vat,
        "Gesamtsumme", f"{period[0]} - {period[1]}", "inkl. USt.", gross,
        "IBAN AT00 0000 0000 0000 0000 zum", "Fälligkeitsdatum 29.04.2030 ein.",  # privacy: allow (placeholder)
    ]
    return toks
