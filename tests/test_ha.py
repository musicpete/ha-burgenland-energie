"""Home Assistant level tests: config flow, sensors, calendar, statistics, actions."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.burgenland_energie.api import (
    AccountData,
    Bill,
    BurgenlandEnergieAuthError,
    Contract,
    Instalment,
    Tariff,
)
from custom_components.burgenland_energie.const import (
    CONF_CUSTOMER_NR,
    CONF_REFRESH_TOKEN,
    CONF_SAVE_INVOICES,
    DOMAIN,
)

TOKEN = "rt-" + "x" * 1800
NEW_TOKEN = "rt-new-" + "y" * 1800
METER_A = Tariff("00000000001", "AT0000000000000000000000000000001", "Tarif A", "T-A", 0.15, 3.0, slug="tarif_a")
METER_B = Tariff("00000000001", "AT0000000000000000000000000000002", "Tarif B Ä", "T-B", 0.125, 0.0, slug="tarif_b_ae")


def _account() -> AccountData:
    today = dt_util.now().date()
    return AccountData(
        customer_nr="000001",
        contracts=[Contract("00000000001", True, (METER_A, METER_B))],
        bills=[
            Bill("000000000003", "00000000001", date(2025, 1, 1), date(2025, 12, 31), 1200.0),
            Bill("000000000002", "00000000001", date(2025, 3, 15), None, 40.0),
        ],
        instalments=[
            Instalment(today - timedelta(days=20), 100.0, 100.0, "partiallyPaid"),
            Instalment(today + timedelta(days=10), 100.0, 0.0, "open"),
            Instalment(today + timedelta(days=40), 100.0, 0.0, "open"),
        ],
        bonus_points=1500,
        bonus_factor=0.1,
    )


class FakeClient:
    instances: list["FakeClient"] = []
    fail_auth = False

    def __init__(self, session, refresh_token, on_refresh_token=None, base_url=None):
        self.refresh_token = refresh_token
        self._cb = on_refresh_token
        self.customer_nr = None
        self.pdf_calls: list[str] = []
        FakeClient.instances.append(self)

    async def async_refresh(self):
        if FakeClient.fail_auth or "bad" in self.refresh_token:
            raise BurgenlandEnergieAuthError("rejected")
        self.customer_nr = "000001"
        return self.customer_nr

    async def async_get_contracts(self):
        await self.async_refresh()
        return _account().contracts

    async def async_get_account(self):
        await self.async_refresh()
        if self._cb and self.refresh_token != NEW_TOKEN:
            self.refresh_token = NEW_TOKEN
            self._cb(NEW_TOKEN)
        return _account()

    async def async_get_meter_days(self, tariff, first, last):
        per_day = 10.0 if tariff is METER_A or tariff.meter_id == METER_A.meter_id else 5.0
        out, d = [], first
        while d <= last:
            out.append((d, per_day if d < last else 0.0))  # yesterday not delivered yet
            d += timedelta(days=1)
        return out

    async def async_get_bill_pdf(self, contract_nr, bill_nr):
        self.pdf_calls.append(bill_nr)
        return b"%PDF-1.4 test"


@pytest.fixture(autouse=True)
def fake_client():
    FakeClient.instances = []
    FakeClient.fail_auth = False
    with (
        patch("custom_components.burgenland_energie.BurgenlandEnergieClient", FakeClient),
        patch("custom_components.burgenland_energie.config_flow.BurgenlandEnergieClient", FakeClient),
        patch("custom_components.burgenland_energie.STARTUP_DELAY_S", 3600),
    ):
        yield


@pytest.fixture
async def vienna(hass: HomeAssistant):
    await hass.config.async_set_time_zone("Europe/Vienna")
    yield


async def _setup(hass: HomeAssistant, options=None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="000001",
        data={CONF_REFRESH_TOKEN: TOKEN, CONF_CUSTOMER_NR: "000001"},
        options=options or {},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_config_flow(recorder_mock, enable_custom_integrations, hass: HomeAssistant, vienna) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_REFRESH_TOKEN: "short"})
    assert result["errors"] == {"base": "invalid_token"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_REFRESH_TOKEN: "bad" + "x" * 200})
    assert result["errors"] == {"base": "invalid_auth"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_REFRESH_TOKEN: f"  {TOKEN}\n"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_REFRESH_TOKEN: TOKEN, CONF_CUSTOMER_NR: "000001"}
    assert result["result"].unique_id == "000001"

    again = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    again = await hass.config_entries.flow.async_configure(again["flow_id"], {CONF_REFRESH_TOKEN: TOKEN})
    assert again["type"] is FlowResultType.ABORT and again["reason"] == "already_configured"


async def test_sensors_calendar_and_token_storage(recorder_mock, enable_custom_integrations, hass: HomeAssistant, vienna) -> None:
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.LOADED
    # The renewed refresh token was written back to the entry.
    assert entry.data[CONF_REFRESH_TOKEN] == NEW_TOKEN

    st = hass.states.get("sensor.burgenland_energie_naechste_teilzahlung")
    assert float(st.state) == 100.0
    assert st.attributes["verbleibende_teilzahlungen"] == 2
    assert st.attributes["summe_verbleibend"] == 200.0
    nxt = hass.states.get("sensor.burgenland_energie_naechste_teilzahlung_am")
    assert nxt.state == (dt_util.now().date() + timedelta(days=10)).isoformat()

    bill = hass.states.get("sensor.burgenland_energie_letzte_rechnung")
    assert float(bill.state) == 1200.0
    assert bill.attributes["jahresabrechnung"] is True
    assert bill.attributes["zeitraum_bis"] == "2025-12-31"
    assert hass.states.get("sensor.burgenland_energie_letzte_rechnung_am").state == "2025-12-31"
    assert hass.states.get("sensor.burgenland_energie_bonuspunkte").state == "1500"

    price = hass.states.get("sensor.burgenland_energie_tarif_a_arbeitspreis")
    assert float(price.state) == pytest.approx(0.18)
    assert price.attributes["netto"] == 0.15
    assert float(hass.states.get("sensor.burgenland_energie_tarif_b_ae_arbeitspreis_netto").state) == 0.125
    base = hass.states.get("sensor.burgenland_energie_tarif_a_grundpreis")
    assert float(base.state) == pytest.approx(3.6)
    assert base.attributes["brutto_pro_jahr"] == pytest.approx(43.2, abs=0.01)

    cal = hass.states.get("calendar.burgenland_energie_teilzahlungen")
    assert cal is not None and "100,00 €" in cal.attributes["message"]
    start = dt_util.now() - timedelta(days=60)
    events = await hass.services.async_call(
        "calendar",
        "get_events",
        {"entity_id": cal.entity_id, "start_date_time": start, "end_date_time": start + timedelta(days=120)},
        blocking=True,
        return_response=True,
    )
    assert len(events[cal.entity_id]["events"]) == 3


async def test_meter_import_writes_daily_statistics(recorder_mock, enable_custom_integrations, hass: HomeAssistant, vienna) -> None:
    await _setup(hass)
    await hass.services.async_call(DOMAIN, "import_meter_data", {"days": 10}, blocking=True)
    await async_wait_recording_done(hass)

    stat_id = f"{DOMAIN}:zp_000001_bezug"
    stats = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utcnow() - timedelta(days=30),
        None,
        {stat_id},
        "hour",
        None,
        {"sum", "state"},
    )
    rows = stats[stat_id]
    assert len(rows) == 10
    assert rows[-1]["sum"] == pytest.approx(90.0)  # 9 delivered days, yesterday 0

    status = hass.states.get("sensor.burgenland_energie_tarif_a_letzter_messtag")
    assert status.state == (dt_util.now().date() - timedelta(days=2)).isoformat()
    assert status.attributes["kwh"] == 10.0
    assert status.attributes["statistik_id"] == stat_id

    # Re-import a shorter window: sums continue from the row before it.
    await hass.services.async_call(DOMAIN, "import_meter_data", {"days": 3}, blocking=True)
    await async_wait_recording_done(hass)
    stats = await hass.async_add_executor_job(
        statistics_during_period, hass, dt_util.utcnow() - timedelta(days=30), None, {stat_id}, "hour", None, {"sum"}
    )
    assert stats[stat_id][-1]["sum"] == pytest.approx(90.0)
    assert len(stats[stat_id]) == 10


async def test_download_invoice_action(recorder_mock, enable_custom_integrations, hass: HomeAssistant, vienna, tmp_path) -> None:
    hass.config.media_dirs = {"local": str(tmp_path)}
    await _setup(hass)
    resp = await hass.services.async_call(DOMAIN, "download_invoice", {}, blocking=True, return_response=True)
    path = tmp_path / "burgenland_energie" / "2025-01-01_Jahresabrechnung_000000000003.pdf"
    assert resp["path"] == str(path)
    assert path.read_bytes().startswith(b"%PDF")


async def test_save_invoices_option(recorder_mock, enable_custom_integrations, hass: HomeAssistant, vienna, tmp_path) -> None:
    hass.config.media_dirs = {"local": str(tmp_path)}
    await _setup(hass, options={CONF_SAVE_INVOICES: True})
    files = sorted(p.name for p in (tmp_path / "burgenland_energie").iterdir())
    assert files == [
        "2025-01-01_Jahresabrechnung_000000000003.pdf",
        "2025-03-15_Rechnung_000000000002.pdf",
    ]
    client = FakeClient.instances[-1]
    await hass.config_entries.async_get_entry(hass.config_entries.async_entries(DOMAIN)[0].entry_id).runtime_data.coordinator.async_refresh()
    assert len(client.pdf_calls) == 2  # existing files are not fetched again


async def test_rejected_token_starts_reauth(recorder_mock, enable_custom_integrations, hass: HomeAssistant, vienna) -> None:
    FakeClient.fail_auth = True
    entry = MockConfigEntry(domain=DOMAIN, unique_id="000001", data={CONF_REFRESH_TOKEN: TOKEN, CONF_CUSTOMER_NR: "000001"})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)

    FakeClient.fail_auth = False
    flow = next(f for f in flows if f["context"]["source"] == "reauth")
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {CONF_REFRESH_TOKEN: "fresh-" + "z" * 300})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"


async def test_actions_are_admin_only(recorder_mock, enable_custom_integrations, hass: HomeAssistant, vienna, hass_read_only_user) -> None:
    from homeassistant.core import Context
    from homeassistant.exceptions import Unauthorized

    await _setup(hass)
    for service in ("download_invoice", "import_meter_data"):
        with pytest.raises(Unauthorized):
            await hass.services.async_call(
                DOMAIN, service, {}, blocking=True, context=Context(user_id=hass_read_only_user.id),
                return_response=service == "download_invoice",
            )


async def test_invoice_file_is_owner_only(recorder_mock, enable_custom_integrations, hass: HomeAssistant, vienna, tmp_path) -> None:
    hass.config.media_dirs = {"local": str(tmp_path)}
    await _setup(hass)
    resp = await hass.services.async_call(DOMAIN, "download_invoice", {}, blocking=True, return_response=True)
    import os, stat

    assert stat.S_IMODE(os.stat(resp["path"]).st_mode) == 0o600
