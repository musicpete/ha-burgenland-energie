"""Burgenland Energie customer portal (meine.burgenlandenergie.at).

Unofficial integration: renews the portal session with a refresh token the
user hands over once and exposes tariffs, the part-payment plan, invoices and
bonus points; imports billed daily energy per meter into long-term statistics
(burgenland_energie:<zaehler>_bezug) and can save invoice PDFs to the media
folder.
"""

from __future__ import annotations

import logging
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
from homeassistant.helpers.update_coordinator import TimestampDataUpdateCoordinator, UpdateFailed

from .api import (
    AccountData,
    Bill,
    BurgenlandEnergieAuthError,
    BurgenlandEnergieClient,
    BurgenlandEnergieError,
)
from .const import (
    ATTR_ALL_MISSING,
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
from . import invoices
from .importer import MeterImporter

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR, Platform.CALENDAR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class BurgenlandEnergieRuntime:
    client: BurgenlandEnergieClient
    coordinator: TimestampDataUpdateCoordinator[AccountData]
    importer: MeterImporter


type BurgenlandEnergieConfigEntry = ConfigEntry[BurgenlandEnergieRuntime]


async def async_save_invoice(
    hass: HomeAssistant, client: BurgenlandEnergieClient, bill: Bill
) -> Path:
    pdf = await client.async_get_bill_pdf(bill.contract_nr, bill.bill_nr)
    return await hass.async_add_executor_job(
        invoices.write_pdf, invoices.invoice_dir(hass), invoices.invoice_filename(bill), pdf
    )


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
        if call.data.get(ATTR_ALL_MISSING):
            saved: list[str] = []
            for entry in _entries(hass):
                rt = entry.runtime_data
                data = rt.coordinator.data
                if not data:
                    continue
                folder = invoices.invoice_dir(hass)
                have = await hass.async_add_executor_job(invoices.verified_files, folder, data.bills)
                for bill in [b for b in data.bills if b.bill_nr not in have]:
                    try:
                        saved.append((await async_save_invoice(hass, rt.client, bill)).name)
                    except BurgenlandEnergieAuthError as err:
                        entry.async_start_reauth(hass)
                        raise HomeAssistantError(f"Re-authentication required: {err}") from err
                    except (BurgenlandEnergieError, OSError) as err:
                        raise HomeAssistantError(f"Invoice download failed: {err}") from err
                await rt.coordinator.async_request_refresh()
            return {"saved": saved}
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
            await rt.coordinator.async_request_refresh()
            return {"path": str(path), "bill_nr": bill.bill_nr, "amount_due": bill.amount}
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
        schema=vol.Schema(
            {vol.Optional(ATTR_BILL_NR): cv.string, vol.Optional(ATTR_ALL_MISSING, default=False): cv.boolean}
        ),
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

    totals_cache: dict = {}

    async def _update() -> AccountData:
        try:
            data = await client.async_get_account()
        except BurgenlandEnergieAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except BurgenlandEnergieError as err:
            raise UpdateFailed(str(err)) from err
        folder = invoices.invoice_dir(hass)
        if entry.options.get(CONF_SAVE_INVOICES):
            await _save_new_invoices(data, folder)
        data.invoice_files = await hass.async_add_executor_job(invoices.verified_files, folder, data.bills)
        data.invoice_links = invoices.signed_links(hass, list(data.invoice_files))
        data.invoice_totals = await hass.async_add_executor_job(
            invoices.read_totals, folder, data.invoice_files, totals_cache
        )
        return data

    async def _save_new_invoices(data: AccountData, folder) -> None:
        have = await hass.async_add_executor_job(invoices.verified_files, folder, data.bills)
        missing = [b for b in data.bills if b.bill_nr not in have][:MAX_INVOICE_DOWNLOADS_PER_RUN]
        for bill in missing:
            try:
                path = await async_save_invoice(hass, client, bill)
                _LOGGER.info("Saved invoice %s", path.name)
            except BurgenlandEnergieError as err:
                _LOGGER.warning("Saving invoice %s failed: %s", bill.bill_nr, err)
            except OSError as err:
                _LOGGER.warning("Cannot write invoice to %s: %s", folder, err)
                return

    coordinator: TimestampDataUpdateCoordinator[AccountData] = TimestampDataUpdateCoordinator(
        hass,
        _LOGGER,
        config_entry=entry,
        name=DOMAIN,
        update_method=_update,
        update_interval=timedelta(hours=POLL_INTERVAL_H),
    )
    invoices.async_register_view(hass)
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
    importer.schedule_totals(coordinator.data.tariffs)
    entry.async_on_unload(importer.async_shutdown)
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
