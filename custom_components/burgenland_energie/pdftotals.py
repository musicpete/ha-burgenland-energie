"""Read the settlement totals from an invoice PDF.

The portal API only reports the amount still to pay (balance plus the first
new part payment). The real cost of a billing period is printed on the
invoice itself, so this module extracts it from the saved PDF.

Deliberately dependency-free: it inflates the content streams, maps glyph
codes back to text through the embedded ToUnicode CMaps and then looks for
the fixed labels of the settlement summary. Nothing is written anywhere;
callers keep only the resulting numbers.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass
from datetime import date

_STREAM = re.compile(rb"stream\r?\n")
_TEXT_OP = re.compile(
    rb"\[((?:[^\]\\]|\\.)*)\]\s*TJ|\(((?:\\.|[^\\)])*)\)\s*Tj|<([0-9A-Fa-f\s]*)>\s*Tj", re.S
)
_LITERAL = re.compile(rb"\(((?:\\.|[^\\)])*)\)|<([0-9A-Fa-f\s]*)>", re.S)
_ESCAPE = re.compile(rb"\\([0-7]{1,3}|.)", re.S)
_ESCAPES = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}
_HEX = re.compile(rb"<([0-9A-Fa-f]+)>")
_AMOUNT = re.compile(r"^-?\d{1,3}(?:\.\d{3})*,\d{2}$")
_DATE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
_GOOD = re.compile(r"[A-Za-z0-9 .,%€äöüÄÖÜß+\-]")

MAX_STREAM = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class InvoiceTotals:
    """Settlement summary of one invoice, amounts in EUR."""

    period_from: date | None
    period_to: date | None
    gross: float
    net: float | None
    vat: float | None
    paid: float | None
    balance: float | None  # > 0 additional payment, < 0 credit

    def as_dict(self) -> dict:
        return {
            "von": self.period_from.isoformat() if self.period_from else None,
            "bis": self.period_to.isoformat() if self.period_to else None,
            "brutto": self.gross,
            "netto": self.net,
            "ust": self.vat,
            "teilbetraege": self.paid,
            "saldo": self.balance,
        }


def _streams(pdf: bytes) -> list[bytes]:
    out: list[bytes] = []
    pos = 0
    while (m := _STREAM.search(pdf, pos)) is not None:
        start = m.end()
        end = pdf.find(b"endstream", start)
        if end < 0:
            break
        pos = end + 9
        if pdf[max(0, m.start() - 3) : m.start()] == b"end":
            continue
        try:
            data = zlib.decompressobj().decompress(pdf[start:end], MAX_STREAM)
        except zlib.error:
            continue
        if data:
            out.append(data)
    return out


def _parse_cmap(data: bytes) -> tuple[dict[int, str], int]:
    """ToUnicode CMap -> (code -> text, code width in bytes)."""
    mapping: dict[int, str] = {}
    width = 1
    if m := re.search(rb"begincodespacerange\s*<([0-9A-Fa-f]+)>", data):
        width = max(1, len(m.group(1)) // 2)

    def _text(hexs: bytes) -> str:
        raw = bytes.fromhex(hexs.decode())
        return raw.decode("utf-16-be", "replace") if len(raw) >= 2 else raw.decode("latin-1")

    for block in re.findall(rb"beginbfchar(.*?)endbfchar", data, re.S):
        hx = _HEX.findall(block)
        for src, dst in zip(hx[0::2], hx[1::2]):
            mapping[int(src, 16)] = _text(dst)
    for block in re.findall(rb"beginbfrange(.*?)endbfrange", data, re.S):
        for a, b, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            lo, hi, base = int(a, 16), int(b, 16), int(dst, 16)
            if hi - lo > 0xFFFF:
                continue
            for code in range(lo, hi + 1):
                mapping[code] = chr(base + code - lo) if base + code - lo < 0x110000 else "�"
    return mapping, width


def _unescape(raw: bytes) -> bytes:
    def rep(m: re.Match[bytes]) -> bytes:
        tok = m.group(1)
        if tok[:1].isdigit():
            return bytes([int(tok, 8) & 0xFF])
        if tok in (b"\n", b"\r"):
            return b""
        return _ESCAPES.get(tok, tok)

    return _ESCAPE.sub(rep, raw)


def _decode(codes: bytes, cmaps: list[tuple[dict[int, str], int]]) -> str:
    """Pick the CMap that turns the codes into the most plausible text."""
    best, score = "", -(10**9)
    for mapping, width in cmaps:
        chars = []
        for i in range(0, len(codes) - width + 1, width):
            chars.append(mapping.get(int.from_bytes(codes[i : i + width], "big"), "�"))
        text = "".join(chars)
        sc = len(_GOOD.findall(text)) - 3 * text.count("�")
        if sc > score:
            best, score = text, sc
    if not cmaps:
        best = codes.decode("latin-1")
    return best.strip()


def text_tokens(pdf: bytes) -> list[str]:
    """Text runs of all pages in content-stream order."""
    streams = _streams(pdf)
    cmaps = [_parse_cmap(s) for s in streams if b"beginbfchar" in s or b"beginbfrange" in s]
    tokens: list[str] = []
    for data in streams:
        if b"BT" not in data or (b"Tj" not in data and b"TJ" not in data):
            continue
        for m in _TEXT_OP.finditer(data):
            if m.group(1) is not None:
                parts = []
                for lit in _LITERAL.finditer(m.group(1)):
                    if lit.group(1) is not None:
                        parts.append(_unescape(lit.group(1)))
                    else:
                        parts.append(bytes.fromhex(re.sub(rb"\s", b"", lit.group(2)).decode() or ""))
                codes = b"".join(parts)
            elif m.group(2) is not None:
                codes = _unescape(m.group(2))
            else:
                codes = bytes.fromhex(re.sub(rb"\s", b"", m.group(3)).decode() or "")
            if text := _decode(codes, cmaps):
                tokens.append(text)
    return tokens


def _amount(tok: str) -> float | None:
    tok = tok.replace("€", "").strip()
    if not _AMOUNT.match(tok):
        return None
    return float(tok.replace(".", "").replace(",", "."))


def _next_amount(tokens: list[str], start: int, limit: int = 8) -> tuple[float | None, int]:
    for i in range(start, min(len(tokens), start + limit)):
        if (val := _amount(tokens[i])) is not None:
            return val, i
    return None, start


def _dates(tokens: list[str], start: int, limit: int = 6) -> list[date]:
    found: list[date] = []
    for tok in tokens[start : start + limit]:
        for d, mo, y in _DATE.findall(tok):
            try:
                found.append(date(int(y), int(mo), int(d)))
            except ValueError:
                continue
        if len(found) >= 2:
            break
    return found[:2]


def _find(tokens: list[str], pred, start: int = 0) -> int:
    for i in range(start, len(tokens)):
        if pred(tokens[i]):
            return i
    return -1


def parse_totals(pdf: bytes) -> InvoiceTotals | None:
    """Settlement totals, or None if the PDF has no settlement summary."""
    try:
        tokens = text_tokens(pdf)
    except (ValueError, MemoryError):
        return None
    net = vat = gross = paid = balance = None
    period: list[date] = []
    anchor = -1

    # Layout A: "Summe exkl. USt." net, "+20 % USt." vat, ... "inkl. USt." gross
    i = _find(tokens, lambda t: t == "Summe exkl. USt.")
    if i >= 0:
        net, j = _next_amount(tokens, i + 1, 3)
        k = _find(tokens, lambda t: t.startswith("+") and "USt" in t, j)
        if net is not None and 0 <= k - j <= 2:
            vat, j = _next_amount(tokens, k + 1, 3)
            k = _find(tokens, lambda t: t == "inkl. USt.", j)
            if vat is not None and 0 <= k - j <= 8:
                gross, j = _next_amount(tokens, k + 1, 2)
                period = _dates(tokens, i, k - i)
                anchor = j
        if gross is None:
            net = vat = None

    # Layout B: "Ihre Gesamtkosten" <period> gross "abzüglich bezahlte Teilbeträge" paid
    if gross is None:
        i = _find(tokens, lambda t: t.startswith("Ihre Gesamtkosten"))
        if i >= 0:
            period = _dates(tokens, i, 3)
            gross, anchor = _next_amount(tokens, i + 1, 5)

    if gross is None:
        return None
    if len(period) < 2:
        for idx, tok in enumerate(tokens):
            if "Gesamtkosten" in tok or tok.startswith("Abrechnungszeitraum"):
                if len(found := _dates(tokens, idx, 4)) == 2:
                    period = found
                    break

    k = _find(tokens, lambda t: t.startswith("Teilbetr") or "bezahlte Teilbetr" in t, anchor)
    if 0 <= k - anchor <= 6:
        val, _ = _next_amount(tokens, k + 1, 3)
        if val is not None:
            paid = abs(val)
    k = _find(tokens, lambda t: t in ("Nachzahlung", "Guthaben"), anchor)
    if 0 <= k - anchor <= 40:
        val, _ = _next_amount(tokens, k + 1, 2)
        if val is not None:
            balance = -abs(val) if tokens[k] == "Guthaben" else abs(val)

    if gross <= 0 or (net is not None and vat is not None and abs(net + vat - gross) > 0.05):
        return None
    return InvoiceTotals(
        period_from=period[0] if period else None,
        period_to=period[1] if len(period) > 1 else None,
        gross=gross,
        net=net,
        vat=vat,
        paid=paid,
        balance=balance,
    )
