"""Burgenland Energie customer portal (meine.burgenlandenergie.at).

Unofficial integration: renews the portal session with a refresh token the
user hands over once and exposes tariffs, the part-payment plan, invoices and
bonus points; imports billed daily energy per meter into long-term statistics
(burgenland_energie:<zaehler>_bezug) and can save invoice PDFs to the media
folder.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.event import async_call_later, async_track_time_change
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    AccountData,
    Bill,
    BurgenlandEnergieAuthError,
    BurgenlandEnergieClient,
    BurgenlandEnergieError,
)
from .const import (
    ATTR_BILL_NR,
    ATTR_DAYS,
    CONF_METER_DAYS,
    CONF_REFRESH_TOKEN,
    CONF_SAVE_INVOICES,
    DEFAULT_METER_DAYS,
    DOMAIN,
    INVOICE_DIR,
    MAX_INVOICE_DOWNLOADS_PER_RUN,
    METER_IMPORT_TIME,
    POLL_INTERVAL_H,
    SERVICE_DOWNLOAD_INVOICE,
    SERVICE_IMPORT_METER_DATA,
    STARTUP_DELAY_S,
)
from .importer import MeterImporter

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR, Platform.CALENDAR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class BurgenlandEnergieRuntime:
    client: BurgenlandEnergieClient
    coordinator: DataUpdateCoordinator[AccountData]
    importer: MeterImporter


type BurgenlandEnergieConfigEntry = ConfigEntry[BurgenlandEnergieRuntime]


def invoice_dir(hass: HomeAssistant) -> Path:
    """<media>/burgenland_energie - the "local" media source if configured."""
    media = hass.config.media_dirs.get("local") or next(iter(hass.config.media_dirs.values()), None)
    return Path(media or hass.config.path("media")) / INVOICE_DIR


def invoice_filename(bill: Bill) -> str:
    # bill_nr is validated as alphanumeric in api.parse_bills.
    kind = "Jahresabrechnung" if bill.is_annual else "Rechnung"
    return f"{bill.date_from.isoformat()}_{kind}_{bill.bill_nr}.pdf"


async def async_save_invoice(
    hass: HomeAssistant, client: BurgenlandEnergieClient, bill: Bill
) -> Path:
    pdf = await client.async_get_bill_pdf(bill.contract_nr, bill.bill_nr)
    folder = invoice_dir(hass)
    path = folder / invoice_filename(bill)

    def _write() -> None:
        # Invoices are personal documents: owner-only permissions on disk.
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(pdf)
        os.replace(tmp, path)

    await hass.async_add_executor_job(_write)
    return path


def _entries(hass: HomeAssistant) -> list[BurgenlandEnergieConfigEntry]:
    return [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if getattr(e, "runtime_data", None) is not None
    ]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register actions once for all entries."""

    async def _download(call: ServiceCall) -> ServiceResponse:
        wanted = call.data.get(ATTR_BILL_NR)
        for entry in _entries(hass):
            rt = entry.runtime_data
            bills = rt.coordinator.data.bills if rt.coordinator.data else []
            bill = next((b for b in bills if b.bill_nr == wanted), None) if wanted else (bills[0] if bills else None)
            if bill is None:
                continue
            try:
                path = await async_save_invoice(hass, rt.client, bill)
            except BurgenlandEnergieAuthError as err:
                entry.async_start_reauth(hass)
                raise HomeAssistantError(f"Re-authentication required: {err}") from err
            except BurgenlandEnergieError as err:
                raise HomeAssistantError(f"Invoice download failed: {err}") from err
            return {"path": str(path), "bill_nr": bill.bill_nr, "amount": bill.amount}
        raise HomeAssistantError("Invoice not found" if wanted else "No invoices available")

    async def _import(call: ServiceCall) -> None:
        days = int(call.data.get(ATTR_DAYS, DEFAULT_METER_DAYS))
        failed: list[str] = []
        for entry in _entries(hass):
            rt = entry.runtime_data
            tariffs = rt.coordinator.data.tariffs if rt.coordinator.data else []
            try:
                failed += await rt.importer.async_import(tariffs, days)
            except BurgenlandEnergieAuthError as err:
                entry.async_start_reauth(hass)
                raise HomeAssistantError(f"Re-authentication required: {err}") from err
        if failed:
            raise HomeAssistantError(f"Import failed for: {', '.join(failed)}")

    # Admin-only: these write files and trigger portal requests.
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_DOWNLOAD_INVOICE,
        _download,
        schema=vol.Schema({vol.Optional(ATTR_BILL_NR): cv.string}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_IMPORT_METER_DATA,
        _import,
        schema=vol.Schema(
            {vol.Optional(ATTR_DAYS, default=DEFAULT_METER_DAYS): vol.All(vol.Coerce(int), vol.Range(1, 2000))}
        ),
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: BurgenlandEnergieConfigEntry) -> bool:
    @callback
    def _store_token(token: str) -> None:
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_REFRESH_TOKEN: token})

    # Own session without a cookie jar: the API needs no cookies.
    client = BurgenlandEnergieClient(
        async_create_clientsession(hass, cookie_jar=aiohttp.DummyCookieJar()),
        entry.data[CONF_REFRESH_TOKEN],
        _store_token,
    )
    importer = MeterImporter(hass, client)

    async def _update() -> AccountData:
        try:
            data = await client.async_get_account()
        except BurgenlandEnergieAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except BurgenlandEnergieError as err:
            raise UpdateFailed(str(err)) from err
        if entry.options.get(CONF_SAVE_INVOICES):
            await _save_new_invoices(data)
        return data

    async def _save_new_invoices(data: AccountData) -> None:
        folder = invoice_dir(hass)
        missing = [
            b
            for b in data.bills
            if not await hass.async_add_executor_job((folder / invoice_filename(b)).exists)
        ][:MAX_INVOICE_DOWNLOADS_PER_RUN]
        for bill in missing:
            try:
                path = await async_save_invoice(hass, client, bill)
                _LOGGER.info("Saved invoice %s", path.name)
            except BurgenlandEnergieError as err:
                _LOGGER.warning("Saving invoice %s failed: %s", bill.bill_nr, err)
            except OSError as err:
                _LOGGER.warning("Cannot write invoice to %s: %s", folder, err)
                return

    coordinator: DataUpdateCoordinator[AccountData] = DataUpdateCoordinator(
        hass,
        _LOGGER,
        config_entry=entry,
        name=DOMAIN,
        update_method=_update,
        update_interval=timedelta(hours=POLL_INTERVAL_H),
    )
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = BurgenlandEnergieRuntime(client, coordinator, importer)

    async def _run_import(*_args) -> None:
        if not coordinator.data:
            return
        days = int(entry.options.get(CONF_METER_DAYS, DEFAULT_METER_DAYS))
        try:
            await importer.async_import(coordinator.data.tariffs, days)
        except BurgenlandEnergieAuthError:
            entry.async_start_reauth(hass)

    hour, minute = METER_IMPORT_TIME
    entry.async_on_unload(async_track_time_change(hass, _run_import, hour=hour, minute=minute, second=0))
    entry.async_on_unload(async_call_later(hass, STARTUP_DELAY_S, _run_import))
    options_seen = dict(entry.options)

    async def _entry_updated(hass: HomeAssistant, updated: BurgenlandEnergieConfigEntry) -> None:
        # Also fires when a renewed refresh token is stored: react to options only.
        nonlocal options_seen
        if dict(updated.options) != options_seen:
            options_seen = dict(updated.options)
            await coordinator.async_request_refresh()

    entry.async_on_unload(entry.add_update_listener(_entry_updated))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BurgenlandEnergieConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
