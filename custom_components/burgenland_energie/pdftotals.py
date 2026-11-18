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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

_STREAM = re.compile(rb"stream\r?\n")
_TEXT_OP = re.compile(
    rb"/([^\s/\[\]()<>]+)\s+[-\d.]+\s+Tf"
    # a TJ array may contain "]" inside its strings, so strings are matched as units
    rb"|\[((?:\((?:\\.|[^\\)])*\)|<[0-9A-Fa-f\s]*>|[^\]()<])*)\]\s*TJ"
    rb"|\(((?:\\.|[^\\)])*)\)\s*Tj|<([0-9A-Fa-f\s]*)>\s*Tj",
    re.S,
)
_LITERAL = re.compile(rb"\(((?:\\.|[^\\)])*)\)|<([0-9A-Fa-f\s]*)>", re.S)
_ESCAPE = re.compile(rb"\\([0-7]{1,3}|.)", re.S)
_ESCAPES = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}
_HEX = re.compile(rb"<([0-9A-Fa-f]+)>")
_AMOUNT = re.compile(r"^-?\d{1,3}(?:\.\d{3})*,\d{2}$")
_DATE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
_GOOD = re.compile(r"[A-Za-z0-9 .,%€äöüÄÖÜß+\-]")
_WORD = re.compile(r"^[A-ZÄÖÜ]?[a-zäöüß]{3,}$")

MAX_STREAM = 8 * 1024 * 1024

CMap = tuple[dict[int, str], int]


@dataclass(frozen=True, slots=True)
class InvoiceTotals:
    """Settlement summary of one invoice, amounts in EUR incl. VAT unless noted."""

    period_from: date | None
    period_to: date | None
    total: float  # "Ihre Gesamtkosten": everything charged for the period
    energy_net: float | None  # electricity cost excl. VAT
    energy_vat: float | None
    energy_gross: float | None  # electricity cost incl. VAT
    subsidy: float | None  # state electricity subsidy (positive number)
    paid: float | None  # part payments made in the period
    balance: float | None  # > 0 additional payment, < 0 credit
    billed_kwh: dict[str, float] = field(default_factory=dict)  # tariff name -> kWh

    @property
    def other(self) -> float | None:
        """Charges besides electricity (fees, dunning costs, interest)."""
        if self.energy_gross is None:
            return None
        return round(self.total - self.energy_gross, 2)

    @property
    def effective(self) -> float:
        """What the period really cost: total minus subsidy."""
        return round(self.total - (self.subsidy or 0.0), 2)

    def as_dict(self) -> dict:
        return {
            "von": self.period_from.isoformat() if self.period_from else None,
            "bis": self.period_to.isoformat() if self.period_to else None,
            "brutto": self.total,
            "zuschuss": self.subsidy,
            "kosten": self.effective,
            "strom_netto": self.energy_net,
            "strom_ust": self.energy_vat,
            "strom_brutto": self.energy_gross,
            "sonstiges": self.other,
            "teilbetraege": self.paid,
            "saldo": self.balance,
            "kwh": dict(self.billed_kwh),
        }


# --------------------------------------------------------------- PDF to text


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


def _parse_cmap(data: bytes) -> CMap:
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


def _hex(raw: bytes) -> bytes:
    digits = re.sub(rb"\s", b"", raw)
    if len(digits) % 2:
        digits += b"0"
    return bytes.fromhex(digits.decode())


def _codes(m: re.Match[bytes]) -> bytes:
    if m.group(2) is not None:
        parts = []
        for lit in _LITERAL.finditer(m.group(2)):
            parts.append(_unescape(lit.group(1)) if lit.group(1) is not None else _hex(lit.group(2)))
        return b"".join(parts)
    if m.group(3) is not None:
        return _unescape(m.group(3))
    return _hex(m.group(4))


def _apply(codes: bytes, cmap: CMap) -> str:
    mapping, width = cmap
    return "".join(
        mapping.get(int.from_bytes(codes[i : i + width], "big"), "�")
        for i in range(0, len(codes) - width + 1, width)
    ).strip()


def _score(text: str) -> int:
    sc = len(_GOOD.findall(text)) - 3 * text.count("�")
    if _AMOUNT.match(text) or _DATE.search(text):
        sc += 10
    return sc + 3 * sum(1 for w in text.split(" ") if _WORD.match(w))


def text_tokens(pdf: bytes) -> list[str]:
    """Text runs of all pages in content-stream order.

    Glyph codes only make sense through the font's own ToUnicode CMap. The
    CMap is chosen per content stream and font: the one that turns all runs
    of that font into the most plausible text wins. Choosing per run instead
    lets digits of one font be read through another font's CMap.
    """
    streams = _streams(pdf)
    cmaps = [_parse_cmap(s) for s in streams if b"beginbfchar" in s or b"beginbfrange" in s]
    tokens: list[str] = []
    for data in streams:
        if b"BT" not in data or (b"Tj" not in data and b"TJ" not in data):
            continue
        runs: list[tuple[bytes, bytes]] = []
        font = b""
        for m in _TEXT_OP.finditer(data):
            if m.group(1) is not None:
                font = m.group(1)
            else:
                runs.append((font, _codes(m)))
        if not cmaps:
            tokens.extend(t for _, c in runs if (t := c.decode("latin-1").strip()))
            continue
        pick: dict[bytes, CMap] = {}
        for f in {f for f, _ in runs}:
            own = [c for ff, c in runs if ff == f]
            pick[f] = max(cmaps, key=lambda cm, own=own: sum(_score(_apply(c, cm)) for c in own))
        tokens.extend(t for f, c in runs if (t := _apply(c, pick[f])))
    return tokens


# ------------------------------------------------------------ text to totals


def _amount(tok: str) -> float | None:
    tok = tok.replace("€", "").strip()
    if not _AMOUNT.match(tok):
        return None
    return float(tok.replace(".", "").replace(",", "."))


def _next_amount(tokens: list[str], start: int, limit: int) -> tuple[float | None, int]:
    for i in range(start, min(len(tokens), start + limit)):
        if (val := _amount(tokens[i])) is not None:
            return val, i
    return None, start


def _dates(tokens: list[str], start: int, limit: int) -> list[date]:
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


def _find(tokens: list[str], pred: Callable[[str], bool], start: int = 0, stop: int | None = None) -> int:
    for i in range(max(0, start), min(len(tokens), stop if stop is not None else len(tokens))):
        if pred(tokens[i]):
            return i
    return -1


_KWH_INLINE = re.compile(r"^(?:Strom\s*·\s*)?(.+?)\s*·\s*(\d{1,3}(?:\.\d{3})*)\s*kWh$")
_KWH_NUMBER = re.compile(r"^\d{1,3}(?:\.\d{3})*$")


def _billed_kwh(tokens: list[str]) -> dict[str, float]:
    """Billed kWh per tariff from the cost overview ("<tariff> <kWh> kWh Energiekosten")."""
    out: dict[str, float] = {}
    for i, tok in enumerate(tokens):
        if tok != "Energiekosten" or i < 2:
            continue
        name = kwh = None
        if m := _KWH_INLINE.match(tokens[i - 1]):
            name, kwh = m.group(1), m.group(2)
        elif tokens[i - 1] == "kWh" and _KWH_NUMBER.match(tokens[i - 2]):
            kwh = tokens[i - 2]
            words: list[str] = []
            for back in tokens[max(0, i - 6) : i - 2][::-1]:
                if not back.strip("\ufffd ·"):
                    continue
                if back == "Strom":  # energy type label in front of the tariff name
                    break
                if any(ch.isdigit() for ch in back) or back.endswith(("€", "Euro")):
                    break
                words.insert(0, back)
                if len(words) == 3:
                    break
            name = " ".join(words) if words else None
        if name and kwh and name not in out:
            out[name] = float(kwh.replace(".", ""))
    return out


def _is_total_label(tokens: list[str], i: int) -> bool:
    tok = tokens[i]
    return tok.startswith("Ihre Gesamtkosten") or (tok == "Gesamtkosten" and i > 0 and tokens[i - 1] == "Ihre")


def parse_totals(pdf: bytes) -> InvoiceTotals | None:
    """Settlement totals, or None if the PDF has no settlement summary."""
    try:
        tokens = text_tokens(pdf)
    except (ValueError, MemoryError):
        return None

    # electricity block: "Summe exkl. USt." net, "+20 % USt." vat, "inkl. USt." gross
    net = vat = energy = None
    i = _find(tokens, lambda t: t == "Summe exkl. USt.")
    if i >= 0:
        n, j = _next_amount(tokens, i + 1, 3)
        k = _find(tokens, lambda t: t.startswith("+") and "USt" in t, j, j + 3)
        if n is not None and k >= 0:
            v, j = _next_amount(tokens, k + 1, 3)
            k = _find(tokens, lambda t: t.endswith("inkl. USt."), j, j + 9)
            if v is not None and k >= 0:
                g, _ = _next_amount(tokens, k + 1, 2)
                if g is not None and abs(n + v - g) <= 0.05:
                    net, vat, energy = n, v, g

    # settlement summary: "Ihre Gesamtkosten" <period> "inkl. USt." total, then
    # optional subsidy, the part payments made and the balance
    total = subsidy = paid = balance = None
    period: list[date] = []
    for idx in range(len(tokens)):
        if not _is_total_label(tokens, idx):
            continue
        found = _dates(tokens, idx, 4)
        value, at = _next_amount(tokens, idx + 1, 7)
        if len(found) == 2 and value is not None:
            period, total = found, value
            break
    if total is None:
        if energy is None:
            return None
        total, at = energy, max(i, 0)

    k = _find(tokens, lambda t: t.startswith("Stromkostenzuschuss"), at + 1, at + 4)
    if k >= 0:
        val, _ = _next_amount(tokens, k + 1, 3)
        subsidy = abs(val) if val is not None else None
    k = _find(tokens, lambda t: t in ("Teilbeträge", "abzüglich bezahlte Teilbeträge"), at + 1, at + 9)
    if k >= 0:
        val, _ = _next_amount(tokens, k + 1, 3)
        paid = abs(val) if val is not None else None
    k = _find(tokens, lambda t: t in ("Nachzahlung", "Guthaben"), at + 1, at + 25)
    if k >= 0:
        val, _ = _next_amount(tokens, k + 1, 2)
        if val is not None:
            balance = -abs(val) if tokens[k] == "Guthaben" else abs(val)

    if total <= 0 or (energy is not None and total + 0.05 < energy):
        return None
    return InvoiceTotals(
        period_from=period[0] if period else None,
        period_to=period[1] if len(period) > 1 else None,
        total=total,
        energy_net=net,
        energy_vat=vat,
        energy_gross=energy,
        subsidy=subsidy,
        paid=paid,
        balance=balance,
        billed_kwh=_billed_kwh(tokens),
    )
