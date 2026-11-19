"""Energy community (Fanclub) invoices: parsing, import from IMAP, sensor."""

from __future__ import annotations

import base64
from datetime import date

import pytest
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.burgenland_energie.const import DOMAIN
from custom_components.burgenland_energie.fanclub import (
    billing_year,
    is_fanclub_sender,
    parse_invoice,
    summary,
)
from custom_components.burgenland_energie import parse_uids

from .fanclub_pdf import invoice_tokens, make_pdf
from .test_ha import _setup  # noqa: F401  (fixtures come from test_ha via conftest-less import)
from .test_ha import fake_client, vienna  # noqa: F401

MONTHLY = invoice_tokens(
    "00000101",
    "Profil-A",
    ("01.03.2030", "31.03.2030"),
    [
        ("10", "Strombezug aus Energiegemeinschaft", "0,10", "100,0", "kWh", "10,00|03/2030"),
        ("11", "Teilnahmegebühr Energiegemeinschaft", "4,00", "1,0", "Monat", "4,00|03/2030"),
    ],
    "14,00", "2,80", "16,80",
)
HALF_YEAR_OLD_LAYOUT = invoice_tokens(
    "00000102",
    "Profil- B",  # older invoices break the name after the hyphen
    ("01.01.2029", "31.03.2029"),
    [
        ("10", "Strombezug aus Energiegemeinschaft", "0,10", "10,0", "kWh", "1,00|01/2029"),
        ("11", "Strombezug aus Energiegemeinschaft", "0,10", "20,0", "kWh", "2,00|02/2029"),
        ("12", "Teilnahmegebühr Energiegemeinschaft", "8,00", "3,0", "Monate", "24,00|01/2029 - 03/2029"),
    ],
    "27,00", "5,40", "32,40",
    issued="15.03.2029",
    old_layout=True,
)


def test_parse_monthly_invoice() -> None:
    inv = parse_invoice(make_pdf(MONTHLY))
    assert inv is not None
    assert (inv.nr, inv.pod_name, inv.issued, inv.due) == ("101", "Profil-A", date(2030, 4, 15), date(2030, 4, 29))
    assert (inv.net, inv.vat, inv.gross, inv.kwh) == (14.0, 2.8, 16.8, 100.0)
    assert inv.net_of("gebuehr") == 4.0 and inv.complete
    assert inv.filename() == "2030-03-01_Fanclub_Profil-A_101.pdf"


def test_parse_old_layout_and_split_fee() -> None:
    inv = parse_invoice(make_pdf(HALF_YEAR_OLD_LAYOUT))
    assert inv is not None and inv.complete
    assert inv.pod_name == "Profil-B"
    months = inv.per_month()
    assert months["2029-01"]["kwh"] == 10.0 and months["2029-02"]["gebuehr_netto"] == 8.0
    assert round(sum(m["brutto"] for m in months.values()), 2) == 32.4


def test_summary_by_billing_year() -> None:
    invs = [parse_invoice(make_pdf(MONTHLY)), parse_invoice(make_pdf(HALF_YEAR_OLD_LAYOUT))]
    agg = summary(invs)
    assert set(agg["jahre"]) == {"2028/29", "2029/30"}
    assert agg["jahre"]["2029/30"]["brutto"] == 16.8
    assert agg["jahre"]["2028/29"]["kwh"] == {"Profil-B": 30.0}
    assert billing_year("2030-06") == "2029/30" and billing_year("2030-07") == "2030/31"


def test_not_an_invoice_and_sender_check() -> None:
    assert parse_invoice(make_pdf(["Übersicht", "Betrachtungszeitraum"])) is None
    assert parse_invoice(b"not a pdf") is None
    assert is_fanclub_sender("noreply@fcbe.at")  # privacy: allow (public sender address of the community)
    assert is_fanclub_sender("Fanclub <noreply@fcbe.at>")  # privacy: allow (public sender address of the community)
    assert not is_fanclub_sender("noreply@fcbe.at.example.com")  # privacy: allow (public sender address of the community)
    assert not is_fanclub_sender(None)


def test_parse_uids() -> None:
    assert parse_uids("1-3,7") == ["1", "2", "3", "7"]
    assert parse_uids("5") == ["5"]
    import voluptuous as vol

    for bad in ("", "a", "3-1", "1-500"):
        with pytest.raises(vol.Invalid):
            parse_uids(bad)


MAILS = {
    "1": {
        "sender": "noreply@fcbe.at",  # privacy: allow (public sender address of the community)
        "date": "2030-04-16T08:00:00+02:00",
        "parts": {
            "0": {"content_type": "text/html"},
            "1": {"content_type": "application/pdf", "filename": "RE_1.pdf"},
            "2": {"content_type": "application/pdf", "filename": "Uebersicht.pdf"},
        },
    },
    "2": {
        "sender": "someone@example.com",
        "date": "2030-04-17T08:00:00+02:00",
        "parts": {"1": {"content_type": "application/pdf", "filename": "RE_X.pdf"}},
    },
}
PARTS = {
    ("1", "1"): make_pdf(MONTHLY),
    ("1", "2"): make_pdf(["Übersicht", "Betrachtungszeitraum: 01.03.2030 bis 31.03.2030"]),
    ("2", "1"): make_pdf(MONTHLY),
}


def _fake_imap(hass: HomeAssistant) -> list[tuple[str, str]]:
    fetched: list[tuple[str, str]] = []

    async def fetch(call):
        return {**MAILS[call.data["uid"]], "uid": call.data["uid"], "text": ""}

    async def fetch_part(call):
        fetched.append((call.data["uid"], call.data["part"]))
        data = base64.b64encode(PARTS[(call.data["uid"], call.data["part"])]).decode()
        return {"part_data": data, "content_type": "application/pdf", "uid": call.data["uid"], "part": call.data["part"]}

    hass.services.async_register("imap", "fetch", fetch, supports_response=SupportsResponse.ONLY)
    hass.services.async_register("imap", "fetch_part", fetch_part, supports_response=SupportsResponse.ONLY)
    return fetched


async def test_import_from_imap(recorder_mock, enable_custom_integrations, hass: HomeAssistant, vienna, tmp_path) -> None:
    hass.config.media_dirs = {"local": str(tmp_path)}
    await _setup(hass)
    imap = MockConfigEntry(domain="imap", title="placeholder")
    imap.add_to_hass(hass)
    fetched = _fake_imap(hass)

    resp = await hass.services.async_call(
        DOMAIN, "import_fanclub_invoices", {"imap_entry": imap.entry_id, "uid": "1-2"}, blocking=True, return_response=True
    )
    await hass.async_block_till_done()
    by_uid = {m["uid"]: m for m in resp["mails"]}
    assert by_uid["1"]["gespeichert"] == ["2030-03-01_Fanclub_Profil-A_101.pdf", "2030-04-16_Fanclub_Uebersicht_U101.pdf"]
    assert by_uid["2"]["status"] == "kein Fanclub-Absender"
    assert ("2", "1") not in fetched  # attachments of other senders are never downloaded

    folder = tmp_path / "burgenland_energie"
    assert sorted(p.name for p in folder.iterdir()) == [
        "2030-03-01_Fanclub_Profil-A_101.pdf",
        "2030-04-16_Fanclub_Uebersicht_U101.pdf",
    ]
    state = hass.states.get("sensor.burgenland_energie_fanclub_kosten")
    assert state is not None
    rows = state.attributes["rechnungen"]
    assert rows[0]["nr"] == "101" and rows[0]["brutto"] == 16.8 and rows[0]["vollstaendig"] is True
    assert rows[0]["pdf"].startswith("/api/burgenland_energie/invoice/101?authSig=")
    assert state.attributes["jahre"]["2029/30"]["kwh"] == {"Profil-A": 100.0}

    # importing again is harmless
    await hass.services.async_call(
        DOMAIN, "import_fanclub_invoices", {"imap_entry": imap.entry_id, "uid": "1"}, blocking=True, return_response=True
    )
    assert len(list(folder.iterdir())) == 2


async def test_import_needs_an_imap_entry(recorder_mock, enable_custom_integrations, hass: HomeAssistant, vienna) -> None:
    await _setup(hass)
    other = MockConfigEntry(domain="template")
    other.add_to_hass(hass)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, "import_fanclub_invoices", {"imap_entry": other.entry_id, "uid": "1"}, blocking=True, return_response=True
        )
