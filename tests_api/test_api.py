"""Client tests against the fake API (no Home Assistant needed)."""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from custom_components.burgenland_energie import api as api_mod
from custom_components.burgenland_energie.api import (
    BurgenlandEnergieAuthError,
    BurgenlandEnergieClient,
    BurgenlandEnergieError,
    slugify,
)

from .fake_api import CONTRACT, CUSTOMER_NR, PDF, POD_A, FakeApi, refresh_token


@pytest.fixture
async def fake():
    fa = FakeApi()
    server = TestServer(fa.app)
    await server.start_server()
    fa.base = str(server.make_url("")).rstrip("/")
    yield fa
    await server.close()


@pytest.fixture(autouse=True)
def no_sleep():
    async def _sleep(_s):
        return None

    with patch.object(api_mod.asyncio, "sleep", _sleep):
        yield


async def _client(fake, token=None, stored=None):
    session = aiohttp.ClientSession()

    def _store(t):
        if stored is not None:
            stored.append(t)

    return BurgenlandEnergieClient(session, token or refresh_token(0), _store, base_url=fake.base), session


async def test_refresh_reads_customer_nr(fake):
    client, session = await _client(fake)
    async with session:
        assert await client.async_refresh() == CUSTOMER_NR


async def test_rejected_token_is_auth_error(fake):
    client, session = await _client(fake, token="invalid" * 30)
    async with session:
        with pytest.raises(BurgenlandEnergieAuthError):
            await client.async_refresh()


async def test_account_parsing(fake):
    client, session = await _client(fake)
    async with session:
        data = await client.async_get_account()
    assert [t.slug for t in data.tariffs] == ["tarif_a", "tarif_b_ae"]
    first = data.tariffs[0]
    assert first.meter_id == "zp_000001"
    assert first.work_price_net == 0.15 and first.work_price_gross == 0.18
    assert first.base_price_gross == pytest.approx(3.6)
    # Newest first; a settlement counts by the end of its billing period.
    assert [b.bill_nr for b in data.bills] == ["000000000003", "000000000002", "000000000001"]
    assert data.bills[0].is_annual and data.bills[0].ref_date == date(2025, 12, 31)
    assert data.bills[1].date_to is None and data.bills[1].ref_date == date(2025, 3, 15)
    assert data.next_instalment(date(2026, 11, 18)).due == date(2026, 12, 1)
    assert data.bonus_points == 1500
    # The second contract has no bills (404) - that must not fail the poll.


async def test_rotated_refresh_token_is_reported(fake):
    fake.rotate = True
    stored: list[str] = []
    client, session = await _client(fake, stored=stored)
    async with session:
        await client.async_refresh()
    assert stored == [refresh_token(1)]
    assert client.refresh_token == refresh_token(1)


async def test_id_token_renewed_when_expired(fake):
    client, session = await _client(fake)
    async with session:
        await client.async_get_contracts()
        assert fake.refresh_calls == 1
        await client.async_get_contracts()
        assert fake.refresh_calls == 1  # still valid
        fake.valid_id.clear()  # server-side expiry -> 401 -> renew once
        await client.async_get_contracts()
        assert fake.refresh_calls == 2


async def test_short_lived_token_renewed_before_use(fake):
    fake.exp_offset = 60  # inside the renewal margin
    client, session = await _client(fake)
    async with session:
        await client.async_get_contracts()
        await client.async_get_contracts()
    assert fake.refresh_calls >= 2


async def test_transient_5xx_retried(fake):
    client, session = await _client(fake)
    async with session:
        await client.async_refresh()
        fake.fail_next = [502, 503]
        assert await client.async_get_contracts()


async def test_persistent_5xx_raises(fake):
    client, session = await _client(fake)
    async with session:
        await client.async_refresh()
        fake.fail_next = [500] * 10
        with pytest.raises(BurgenlandEnergieError):
            await client.async_get_contracts()


async def test_meter_days_chunked_by_year(fake):
    client, session = await _client(fake)
    async with session:
        tariff = (await client.async_get_contracts())[0].tariffs[0]
        assert tariff.pod == POD_A
        days = await client.async_get_meter_days(tariff, date(2024, 1, 1), date(2026, 11, 17))
    assert len(days) == (date(2026, 11, 17) - date(2024, 1, 1)).days + 1
    assert days[0] == (date(2024, 1, 1), 10.0)
    assert sum(1 for c in fake.calls if "meter-data" in c) == 3


async def test_invoice_pdf(fake):
    client, session = await _client(fake)
    async with session:
        await client.async_refresh()
        assert await client.async_get_bill_pdf(CONTRACT, "000000000003") == PDF


def test_slugify():
    assert slugify("Tarif B Ä") == "tarif_b_ae"
    assert slugify("Tarif C Öl+") == "tarif_c_oel"


def test_translations_have_no_tags():
    import json
    import pathlib

    root = pathlib.Path(api_mod.__file__).parent
    for f in [root / "strings.json", *(root / "translations").glob("*.json")]:
        assert "<" not in f.read_text(), f
        json.loads(f.read_text())


async def test_errors_do_not_contain_account_numbers(fake):
    client, session = await _client(fake)
    async with session:
        await client.async_refresh()
        fake.fail_next = [500] * 10
        with pytest.raises(BurgenlandEnergieError) as exc:
            await client.async_get_bills(CONTRACT)
    assert CONTRACT not in str(exc.value)
    assert "/api/contracts/…/bills" in str(exc.value)


async def test_rate_limit_is_not_an_auth_error(fake):
    client, session = await _client(fake)
    async with session:
        await client.async_refresh()
        fake.fail_next = [429, 429]
        assert await client.async_get_contracts()


def test_unsafe_identifiers_are_dropped():
    from custom_components.burgenland_energie.api import parse_bills, parse_contracts

    contracts = parse_contracts(
        [
            {"nr": "../../etc", "isActive": True, "tariffs": []},
            {"nr": "4000", "isActive": True, "tariffs": [{"extUi": "AT/../x", "tariffName": "A"}, {"extUi": "AT1", "tariffName": "B"}]},
        ]
    )
    assert [c.nr for c in contracts] == ["4000"]
    assert [t.pod for t in contracts[0].tariffs] == ["AT1"]
    bills = parse_bills("4000", [{"billNr": "1/../2", "dateFrom": "2025-01-01", "amount": 1}, {"billNr": "12", "dateFrom": "2025-01-01", "amount": 1}])
    assert [b.bill_nr for b in bills] == ["12"]
