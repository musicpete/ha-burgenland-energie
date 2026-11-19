"""Invoices of the energy community "Fanclub Burgenland Energieunabhängig".

The community bills its members separately from Burgenland Energie: one PDF
per point of delivery and billing period, sent by e-mail. Home Assistant's
IMAP integration fetches the mails; this module saves the PDF attachments
next to the Burgenland Energie invoices and reads the amounts back.

Only amounts, dates, quantities and the point-of-delivery name the member
chose are kept. Names, addresses, bank details and mandate references printed
on the invoice are never extracted.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .pdftotals import text_tokens

SENDER_DOMAIN = "fcbe.at"
FILE_TAG = "Fanclub"
OVERVIEW_PREFIX = "U"

_NUM = r"-?\d{1,3}(?:\.\d{3})*,\d{1,4}"
_DATE = r"(\d{2})\.(\d{2})\.(\d{4})"
_UNIT = r"kWh|Monate?|Stk\.?"
_POSITION = re.compile(
    rf"(?<![\d,.])(\d{{2,3}}) ([A-ZÄÖÜ][^\d€]{{3,80}}?) ({_NUM}) ({_NUM}) "
    rf"(?:({_UNIT}) ({_NUM})|({_NUM}) ({_UNIT})) "
    r"Zeitraum: (\d{2})/(\d{4})(?: - (\d{2})/(\d{4}))?"
)
_INVOICE_NR = re.compile(r"Rechnungsnummer (\d{4,20})\b")
_INVOICE_DATE = re.compile(rf"Rechnungsdatum {_DATE}")
_DUE = re.compile(rf"Fälligkeitsdatum {_DATE}")
_POD_NAME = re.compile(r"Zählpunkt Name: (.{1,60}?) (?:Belieferungsstart|Rechnungszeitraum)")
_PERIOD = re.compile(rf"Rechnungszeitraum: {_DATE} bis {_DATE}")
_NET = re.compile(rf"Summe exkl\. USt\. ({_NUM})")
_VAT = re.compile(rf"\+ ?(\d{{1,2}}) ?% USt\. ({_NUM})")
_GROSS = re.compile(rf"Gesamtsumme {_DATE} . {_DATE} inkl\. USt\. ({_NUM})")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9äöüÄÖÜß]+")


def _num(text: str) -> float:
    return float(text.replace(".", "").replace(",", "."))


def _date(d: str, m: str, y: str) -> date | None:
    try:
        return date(int(y), int(m), int(d))
    except ValueError:
        return None


def _months(first: tuple[int, int], last: tuple[int, int]) -> list[str]:
    (y, m), out = first, []
    while (y, m) <= last and len(out) < 36:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


@dataclass(frozen=True, slots=True)
class Position:
    kind: str  # "bezug" (energy), "gebuehr" (participation fee), "einspeisung", "sonstiges"
    text: str
    months: tuple[str, ...]  # "YYYY-MM", one or more
    quantity: float
    unit: str
    price: float
    value: float  # EUR excl. VAT


@dataclass(frozen=True, slots=True)
class FanclubInvoice:
    nr: str
    issued: date
    due: date | None
    pod_name: str  # name the member gave the point of delivery, e.g. "Profil-A"
    period_from: date
    period_to: date
    net: float
    vat: float
    gross: float
    positions: tuple[Position, ...] = field(default_factory=tuple)

    @property
    def complete(self) -> bool:
        """All positions found and the sums add up."""
        return (
            abs(sum(p.value for p in self.positions) - self.net) <= 0.02
            and abs(self.net + self.vat - self.gross) <= 0.02
        )

    @property
    def kwh(self) -> float:
        return round(sum(p.quantity for p in self.positions if p.kind == "bezug"), 1)

    def net_of(self, kind: str) -> float:
        return round(sum(p.value for p in self.positions if p.kind == kind), 2)

    def per_month(self) -> dict[str, dict[str, float]]:
        """Month -> kWh, net and gross amounts; multi-month positions are split evenly."""
        factor = self.gross / self.net if self.net else 1.0
        out: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for p in self.positions:
            share = 1 / len(p.months)
            for month in p.months:
                row = out[month]
                if p.kind == "bezug":
                    row["kwh"] += p.quantity * share
                row[f"{p.kind}_netto"] += p.value * share
                row["netto"] += p.value * share
                row["brutto"] += p.value * share * factor
        return {m: {k: round(v, 3 if k == "kwh" else 2) for k, v in row.items()} for m, row in out.items()}

    def filename(self) -> str:
        pod = _SAFE_NAME.sub("-", self.pod_name).strip("-") or "Zaehlpunkt"
        return f"{self.period_from.isoformat()}_{FILE_TAG}_{pod}_{self.nr}.pdf"


def _kind(text: str) -> str:
    low = text.lower()
    if "teilnahme" in low:
        return "gebuehr"
    if "einspeis" in low or "erzeugung" in low:
        return "einspeisung"
    if "bezug" in low:
        return "bezug"
    return "sonstiges"


def parse_invoice(pdf: bytes) -> FanclubInvoice | None:
    """The invoice, or None if the PDF is not a Fanclub invoice."""
    try:
        text = " ".join(text_tokens(pdf))
    except (ValueError, MemoryError):
        return None
    text = re.sub(r"\s+", " ", text)
    nr, issued, period, net, vat, gross = (
        _INVOICE_NR.search(text),
        _INVOICE_DATE.search(text),
        _PERIOD.search(text),
        _NET.search(text),
        _VAT.search(text),
        _GROSS.search(text),
    )
    if not (nr and issued and period and net and vat and gross):
        return None
    pod = _POD_NAME.search(text)
    due = _DUE.search(text)
    positions: list[Position] = []
    for m in _POSITION.finditer(text):
        label = m.group(2).strip()
        price, qty = _num(m.group(3)), _num(m.group(4))
        unit, value = (m.group(5), m.group(6)) if m.group(5) else (m.group(8), m.group(7))
        value_f = _num(value)
        if abs(price * qty - value_f) > 0.011 + abs(value_f) * 0.001:
            continue  # columns did not line up: rather drop than misread
        first = (int(m.group(10)), int(m.group(9)))
        last = (int(m.group(12)), int(m.group(11))) if m.group(11) else first
        positions.append(
            Position(_kind(label), label, tuple(_months(first, last)), qty, unit, price, value_f)
        )
    issued_d = _date(*issued.groups())
    period_from, period_to = _date(*period.groups()[:3]), _date(*period.groups()[3:])
    if issued_d is None or period_from is None or period_to is None:
        return None
    return FanclubInvoice(
        nr=nr.group(1).lstrip("0") or "0",
        issued=issued_d,
        due=_date(*due.groups()) if due else None,
        pod_name=re.sub(r"-\s+", "-", pod.group(1)).strip() if pod else "",
        period_from=period_from,
        period_to=period_to,
        net=_num(net.group(1)),
        vat=_num(vat.group(2)),
        gross=_num(gross.group(7)),
        positions=tuple(positions),
    )


def is_fanclub_sender(sender: str | None) -> bool:
    addr = (sender or "").strip().lower()
    addr = addr.rsplit("<", 1)[-1].rstrip(">").strip()
    return addr.endswith("@" + SENDER_DOMAIN)


def overview_filename(mail_date: date, first_nr: str) -> str:
    return f"{mail_date.isoformat()}_{FILE_TAG}_Uebersicht_{OVERVIEW_PREFIX}{first_nr}.pdf"


def scan(folder: Path, cache: dict[tuple[str, int, int], FanclubInvoice | None]) -> list[FanclubInvoice]:
    """All saved Fanclub invoices, newest first (blocking). Parsed once per file version."""
    found: dict[str, FanclubInvoice] = {}
    if not folder.is_dir():
        return []
    for path in folder.glob(f"*_{FILE_TAG}_*.pdf"):
        if f"_{FILE_TAG}_Uebersicht_" in path.name or not path.is_file():
            continue
        try:
            st = path.stat()
            key = (path.name, st.st_size, st.st_mtime_ns)
            if key not in cache:
                cache[key] = parse_invoice(path.read_bytes())
        except OSError:
            continue
        if (inv := cache[key]) is not None:
            found[inv.nr] = inv
    return sorted(found.values(), key=lambda i: (i.issued, i.nr), reverse=True)


def billing_year(month: str) -> str:
    """Burgenland Energie bills July to June: '2030-08' -> '2030/31'."""
    y, m = int(month[:4]), int(month[5:7])
    start = y if m >= 7 else y - 1
    return f"{start}/{(start + 1) % 100:02d}"


def summary(invoices: list[FanclubInvoice]) -> dict:
    """Months and billing years aggregated over all invoices and points of delivery."""
    months: dict[str, dict] = defaultdict(lambda: {"kwh": {}, "netto": 0.0, "brutto": 0.0, "gebuehr_netto": 0.0})
    for inv in invoices:
        for month, row in inv.per_month().items():
            m = months[month]
            if row.get("kwh"):
                m["kwh"][inv.pod_name] = round(m["kwh"].get(inv.pod_name, 0.0) + row["kwh"], 1)
            m["netto"] += row.get("netto", 0.0)
            m["brutto"] += row.get("brutto", 0.0)
            m["gebuehr_netto"] += row.get("gebuehr_netto", 0.0)
    years: dict[str, dict] = defaultdict(lambda: {"kwh": {}, "netto": 0.0, "brutto": 0.0, "gebuehr_netto": 0.0, "monate": 0})
    for month, m in months.items():
        y = years[billing_year(month)]
        for pod, kwh in m["kwh"].items():
            y["kwh"][pod] = round(y["kwh"].get(pod, 0.0) + kwh, 1)
        for key in ("netto", "brutto", "gebuehr_netto"):
            y[key] += m[key]
        y["monate"] += 1

    def _round(d: dict) -> dict:
        return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in d.items()}

    return {
        "monate": {k: _round(months[k]) for k in sorted(months)},
        "jahre": {k: _round(years[k]) for k in sorted(years)},
    }
