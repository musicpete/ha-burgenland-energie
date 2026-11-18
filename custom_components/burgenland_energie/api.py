"""Client for the Burgenland Energie customer portal API (meine.burgenlandenergie.at).

No Home Assistant imports here, so the client can be tested on its own.

Authentication: the portal signs in through AWS Cognito behind a Friendly
Captcha, so a password login cannot be automated. The user hands over the
portal's *refresh token* once; it is exchanged for a one-hour id token via
``POST /papi/auth/signin/refresh`` (no captcha), and every new refresh token
the portal returns is handed back to the caller for storage.

API requests send the raw id token in the ``Authorization`` header
(no "Bearer" prefix - the API answers 500 with one).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

API_BASE = "https://api.meine.burgenlandenergie.at"
PORTAL_ORIGIN = "https://meine.burgenlandenergie.at"
VAT_RATE = 0.20  # Austrian VAT on energy prices
TIMEOUT_S = 60
RETRY_DELAYS_S = (2, 5, 15)
TOKEN_MARGIN_S = 300  # renew this long before the id token expires
METER_CHUNK_DAYS = 365  # the API answers a full year in one request
OPEN_END = date(9999, 12, 31)


_ID_RE = re.compile(r"^[A-Za-z0-9]{1,40}$")


def valid_id(value: Any) -> str | None:
    """Contract, invoice and point-of-delivery numbers go into URL paths and
    file names: accept plain alphanumerics only."""
    text = str(value or "").strip()
    return text if _ID_RE.match(text) else None


def redact(path: str) -> str:
    """Endpoint for messages and logs without account identifiers."""
    return re.sub(r"[A-Za-z]*\d{4,}[A-Za-z0-9]*", "…", path.split("?", 1)[0])


class BurgenlandEnergieError(Exception):
    """Portal unreachable or answered something unexpected."""


class BurgenlandEnergieAuthError(BurgenlandEnergieError):
    """Refresh token rejected - the user has to hand over a new one."""


def jwt_claims(token: str) -> dict[str, Any]:
    """Claims of a JWT without verifying it (only used to read our own ids)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, binascii.Error) as err:
        raise BurgenlandEnergieAuthError("Malformed id token") from err


def slugify(text: str) -> str:
    """'Tarif B Ä' -> 'tarif_b_ae' (German umlauts spelled out)."""
    text = text.lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        text = text.replace(a, b)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_") or "tarif"


def _date(value: Any) -> date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Tariff:
    """Energy tariff of one point of delivery (meter). Prices are net."""

    contract_nr: str
    pod: str
    name: str
    tariff_type: str
    work_price_net: float | None  # EUR/kWh
    base_price_net: float | None  # EUR/month
    price_guarantee_until: date | None = None
    bound_until: date | None = None
    community_member: bool = False
    slug: str = ""

    @property
    def meter_id(self) -> str:
        """Short, stable id for entity unique ids and statistic ids."""
        return "zp_" + re.sub(r"[^a-z0-9]", "", self.pod.lower())[-6:]

    @property
    def work_price_gross(self) -> float | None:
        return None if self.work_price_net is None else round(self.work_price_net * (1 + VAT_RATE), 6)

    @property
    def base_price_gross(self) -> float | None:
        return None if self.base_price_net is None else round(self.base_price_net * (1 + VAT_RATE), 4)


@dataclass(frozen=True)
class Contract:
    nr: str
    electricity: bool
    tariffs: tuple[Tariff, ...] = ()


@dataclass(frozen=True)
class Bill:
    bill_nr: str
    contract_nr: str
    date_from: date
    date_to: date | None  # None: the portal shows no billing period
    amount: float
    currency: str = "EUR"

    @property
    def ref_date(self) -> date:
        """End of the billing period for settlements, else the invoice date.

        For a settlement the portal's ``dateFrom`` is the start of the billed
        year, which would sort it a year too early.
        """
        return self.date_to or self.date_from

    @property
    def is_annual(self) -> bool:
        """A settlement with a billing period (Jahresabrechnung)."""
        return self.date_to is not None


@dataclass(frozen=True)
class Instalment:
    """One entry of the part-payment plan (Teilzahlungsplan)."""

    due: date
    amount: float
    amount_open: float | None
    status: str


@dataclass
class AccountData:
    """Everything one poll returns."""

    customer_nr: str
    contracts: list[Contract] = field(default_factory=list)
    bills: list[Bill] = field(default_factory=list)  # newest first
    instalments: list[Instalment] = field(default_factory=list)  # by due date
    bonus_points: int | None = None
    bonus_factor: float | None = None

    @property
    def tariffs(self) -> list[Tariff]:
        return [t for c in self.contracts for t in c.tariffs]

    def next_instalment(self, today: date) -> Instalment | None:
        return next((i for i in self.instalments if i.due >= today), None)


# ---------------------------------------------------------------- parsing


def parse_contracts(raw: Any) -> list[Contract]:
    if not isinstance(raw, list):
        raise BurgenlandEnergieError("Unexpected contracts response")
    contracts: list[Contract] = []
    used_slugs: set[str] = set()
    for c in raw:
        nr = valid_id(c.get("nr")) if isinstance(c, dict) else None
        if not nr or c.get("isActive") is False:
            continue
        tariffs = []
        for t in c.get("tariffs") or []:
            pod = valid_id(t.get("extUi")) if isinstance(t, dict) else None
            name = str(t.get("tariffName") or "").strip()
            if not pod or not name:
                continue
            slug = slugify(name)
            if slug in used_slugs:  # two meters on the same tariff
                slug = f"{slug}_{re.sub(r'[^0-9]', '', pod)[-4:]}"
            used_slugs.add(slug)
            tariffs.append(
                Tariff(
                    contract_nr=nr,
                    pod=pod,
                    name=name,
                    tariff_type=str(t.get("tariffType") or ""),
                    work_price_net=_float(t.get("workPrice")),
                    base_price_net=_float(t.get("basePrice")),
                    price_guarantee_until=_date(t.get("priceGuaranteeUntil")),
                    bound_until=_date(t.get("boundUntil")),
                    community_member=bool(t.get("fcbeMember")),
                    slug=slug,
                )
            )
        contracts.append(Contract(nr, bool(c.get("electricity")), tuple(tariffs)))
    return contracts


def parse_bills(contract_nr: str, raw: Any) -> list[Bill]:
    if not isinstance(raw, list):
        raise BurgenlandEnergieError("Unexpected bills response")
    bills = []
    for b in raw:
        start = _date(b.get("dateFrom")) if isinstance(b, dict) else None
        amount = _float(b.get("amount")) if isinstance(b, dict) else None
        bill_nr = valid_id(b.get("billNr")) if isinstance(b, dict) else None
        if not start or amount is None or not bill_nr:
            continue
        end = _date(b.get("dateTo"))
        bills.append(
            Bill(
                bill_nr=bill_nr,
                contract_nr=contract_nr,
                date_from=start,
                date_to=None if end in (None, OPEN_END) else end,
                amount=amount,
                currency=str(b.get("amountCurr") or "EUR"),
            )
        )
    return bills


def parse_instalments(raw: Any) -> list[Instalment]:
    if not isinstance(raw, dict):
        raise BurgenlandEnergieError("Unexpected part-payment plan response")
    out = []
    for a in raw.get("advancePayment") or []:
        due = _date(a.get("dueDate"))
        amount = _float(a.get("amount"))
        if due and amount is not None:
            out.append(Instalment(due, amount, _float(a.get("amountOpen")), str(a.get("balancingStatus") or "")))
    return sorted(out, key=lambda i: i.due)


def parse_meter_days(raw: Any) -> list[tuple[date, float]]:
    if not isinstance(raw, list):
        raise BurgenlandEnergieError("Unexpected meter data response")
    out = []
    for row in raw:
        day = _date(row.get("description")) if isinstance(row, dict) else None
        value = _float(row.get("value")) if isinstance(row, dict) else None
        if day and value is not None:
            out.append((day, value))
    return sorted(out)


# ----------------------------------------------------------------- client

TokenCallback = Callable[[str], Awaitable[None] | None]


class BurgenlandEnergieClient:
    """Talks to the portal API with a refresh token the user handed over."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        refresh_token: str,
        on_refresh_token: TokenCallback | None = None,
        base_url: str = API_BASE,
    ) -> None:
        self._session = session
        self._refresh_token = refresh_token.strip()
        self._on_refresh_token = on_refresh_token
        self._base = base_url.rstrip("/")
        self._id_token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()
        self.customer_nr: str | None = None

    @property
    def refresh_token(self) -> str:
        return self._refresh_token

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Origin": PORTAL_ORIGIN,
            "Referer": PORTAL_ORIGIN + "/",
        }

    async def async_refresh(self) -> str:
        """Exchange the refresh token for a fresh id token; returns customer nr."""
        async with self._lock:
            await self._refresh_locked()
        assert self.customer_nr
        return self.customer_nr

    async def _refresh_locked(self) -> None:
        url = f"{self._base}/papi/auth/signin/refresh"
        try:
            async with self._session.post(
                url,
                json={"refreshToken": self._refresh_token},
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=TIMEOUT_S),
            ) as resp:
                if resp.status == 429:
                    raise BurgenlandEnergieError("Token renewal rate-limited (HTTP 429)")
                if 400 <= resp.status < 500:
                    raise BurgenlandEnergieAuthError(f"Token renewal rejected (HTTP {resp.status})")
                if resp.status != 200:
                    raise BurgenlandEnergieError(f"Token renewal failed (HTTP {resp.status})")
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise BurgenlandEnergieError(f"Portal unreachable ({type(err).__name__})") from err
        id_token = (data or {}).get("idToken")
        if not id_token:
            raise BurgenlandEnergieAuthError("Token renewal returned no id token")
        claims = jwt_claims(id_token)
        customer_nr = valid_id(claims.get("custom:customerNr"))
        if not customer_nr:
            raise BurgenlandEnergieError("Id token carries no customer number")
        try:
            lifetime = int(data.get("expireIn") or 3600)
        except (TypeError, ValueError):
            lifetime = 3600
        exp = claims.get("exp")
        self._expires_at = float(exp) if isinstance(exp, (int, float)) else time.time() + lifetime
        self._id_token = id_token
        self.customer_nr = customer_nr
        new_refresh = (data.get("refreshToken") or "").strip()
        if new_refresh and new_refresh != self._refresh_token:
            self._refresh_token = new_refresh
            if self._on_refresh_token:
                result = self._on_refresh_token(new_refresh)
                if asyncio.iscoroutine(result):
                    await result

    async def _ensure_token(self, force: bool = False) -> str:
        async with self._lock:
            if force or not self._id_token or time.time() > self._expires_at - TOKEN_MARGIN_S:
                await self._refresh_locked()
            assert self._id_token
            return self._id_token

    async def _request(self, path: str, params: dict[str, str] | None = None) -> Any:
        """GET with token renewal on 401/403 and retries on transient errors.

        Error messages name the endpoint without contract or meter numbers.
        """
        url = f"{self._base}{path}"
        where = redact(path)
        renewed = False
        attempt = 0
        while True:
            token = await self._ensure_token()
            try:
                async with self._session.get(
                    url,
                    params=params,
                    headers={**self._headers(), "Authorization": token},
                    timeout=aiohttp.ClientTimeout(total=TIMEOUT_S),
                ) as resp:
                    if resp.status in (401, 403) and not renewed:
                        renewed = True
                        await self._ensure_token(force=True)
                        continue
                    if resp.status in (401, 403):
                        raise BurgenlandEnergieAuthError(f"Access denied (HTTP {resp.status})")
                    if resp.status >= 500 or resp.status == 429:
                        raise BurgenlandEnergieError(f"HTTP {resp.status} for {where}")
                    if resp.status != 200:
                        raise _NoRetry(f"HTTP {resp.status} for {where}")
                    return await resp.json(content_type=None)
            except _NoRetry as err:
                raise BurgenlandEnergieError(str(err)) from None
            except BurgenlandEnergieAuthError:
                raise
            except (BurgenlandEnergieError, aiohttp.ClientError, TimeoutError, ValueError) as err:
                if attempt >= len(RETRY_DELAYS_S):
                    if isinstance(err, BurgenlandEnergieError):
                        raise
                    raise BurgenlandEnergieError(f"Request failed for {where}: {type(err).__name__}") from err
                delay = RETRY_DELAYS_S[attempt]
                attempt += 1
                _LOGGER.debug("Retrying %s in %ss after %s", where, delay, type(err).__name__)
                await asyncio.sleep(delay)

    # ------------------------------------------------------------ endpoints

    async def async_get_contracts(self) -> list[Contract]:
        if not self.customer_nr:
            await self.async_refresh()
        raw = await self._request(
            "/api/contracts",
            {
                "customerNr": str(self.customer_nr),
                "ega": "true",
                "sd": "true",
                "naturalGas": "true",
                "warmth": "true",
                "eMobility": "true",
                "service": "true",
                "electricityFeeders": "true",
                "onlyActive": "true",
            },
        )
        return parse_contracts(raw)

    async def async_get_bills(self, contract_nr: str) -> list[Bill]:
        return parse_bills(contract_nr, await self._request(f"/api/contracts/{contract_nr}/bills"))

    async def async_get_bill_pdf(self, contract_nr: str, bill_nr: str) -> bytes:
        raw = await self._request(
            f"/api/contracts/{contract_nr}/bills/{bill_nr}", {"compress": "false"}
        )
        try:
            pdf = base64.b64decode((raw or {}).get("data") or "", validate=False)
        except (binascii.Error, AttributeError) as err:
            raise BurgenlandEnergieError("Invoice PDF could not be decoded") from err
        if not pdf.startswith(b"%PDF"):
            raise BurgenlandEnergieError("Invoice download is not a PDF")
        return pdf

    async def async_get_instalments(self, contract_nr: str) -> list[Instalment]:
        raw = await self._request(
            f"/api/contracts/{contract_nr}/part-payment-plan",
            {"customerNr": str(self.customer_nr)},
        )
        return parse_instalments(raw)

    async def async_get_bonus(self) -> tuple[int | None, float | None]:
        raw = await self._request("/api/profile/bonus-points")
        if not isinstance(raw, dict):
            return None, None
        points = raw.get("bonuspoints")
        return (int(points) if isinstance(points, (int, float)) else None), _float(raw.get("factor"))

    async def async_get_meter_days(
        self, tariff: Tariff, first: date, last: date
    ) -> list[tuple[date, float]]:
        """Billed daily energy (kWh) of one meter, in yearly chunks."""
        out: list[tuple[date, float]] = []
        start = first
        while start <= last:
            end = min(last, start + timedelta(days=METER_CHUNK_DAYS - 1))
            raw = await self._request(
                f"/api/contracts/{tariff.contract_nr}/meter-data/{tariff.pod}/v2",
                {"fromDate": start.isoformat(), "toDate": end.isoformat()},
            )
            out.extend(d for d in parse_meter_days(raw) if first <= d[0] <= last)
            start = end + timedelta(days=1)
        return out

    async def async_get_account(self) -> AccountData:
        """One poll: contracts, invoices, part-payment plan and bonus points."""
        contracts = await self.async_get_contracts()
        data = AccountData(customer_nr=str(self.customer_nr), contracts=contracts)
        for c in contracts:
            try:
                data.bills.extend(await self.async_get_bills(c.nr))
            except BurgenlandEnergieAuthError:
                raise
            except BurgenlandEnergieError as err:
                _LOGGER.debug("No invoices for a contract: %s", err)
            if c.electricity:
                try:
                    data.instalments.extend(await self.async_get_instalments(c.nr))
                except BurgenlandEnergieAuthError:
                    raise
                except BurgenlandEnergieError as err:
                    _LOGGER.debug("No part-payment plan for a contract: %s", err)
        data.bills.sort(key=lambda b: (b.ref_date, b.bill_nr), reverse=True)
        data.instalments.sort(key=lambda i: i.due)
        try:
            data.bonus_points, data.bonus_factor = await self.async_get_bonus()
        except BurgenlandEnergieAuthError:
            raise
        except BurgenlandEnergieError as err:
            _LOGGER.debug("No bonus points: %s", err)
        return data


class _NoRetry(Exception):
    """4xx other than auth: retrying won't help."""
