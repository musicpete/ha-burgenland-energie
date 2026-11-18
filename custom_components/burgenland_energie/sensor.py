"""Sensors: tariff per meter, part-payment plan, invoices, bonus points."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import BurgenlandEnergieConfigEntry
from .api import AccountData, Tariff, slugify
from .const import CONF_SAVE_INVOICES, DOMAIN, MANUFACTURER, METER_DATA_STALE_DAYS, SIGNAL_METER_UPDATED
from .importer import MeterImporter, statistic_id

PARALLEL_UPDATES = 0


def _account_device(entry: BurgenlandEnergieConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="Burgenland Energie",
        manufacturer=MANUFACTURER,
        model="Kundenportal",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url="https://meine.burgenlandenergie.at",
    )


def _meter_device(entry: BurgenlandEnergieConfigEntry, tariff: Tariff) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_{tariff.meter_id}")},
        name=f"Burgenland Energie {tariff.name}",
        manufacturer=MANUFACTURER,
        model=tariff.tariff_type or "Stromzähler",
        entry_type=DeviceEntryType.SERVICE,
        via_device=(DOMAIN, entry.entry_id),
    )


# ------------------------------------------------------------- account level


@dataclass(frozen=True, kw_only=True)
class AccountSensorDescription(SensorEntityDescription):
    value: Callable[[AccountData, date], Any]
    attrs: Callable[[AccountData, date], dict[str, Any]] = lambda _d, _t: {}


def _bill_attrs(d: AccountData, _today: date) -> dict[str, Any]:
    """The portal reports the amount to pay per invoice ("Zu zahlender Betrag":
    back-payment or credit plus the first new instalment), not the invoice total."""
    if not d.bills:
        return {}
    b = d.bills[0]
    return {
        "rechnungsnummer": b.bill_nr,
        "datum": b.ref_date.isoformat(),
        "zeitraum_von": b.date_from.isoformat() if b.date_to else None,
        "zeitraum_bis": b.date_to.isoformat() if b.date_to else None,
        "jahresabrechnung": b.is_annual,
        "pdf": d.invoice_links.get(b.bill_nr),
        "rechnungen": [
            {
                "datum": x.ref_date.isoformat(),
                "zahlbetrag": x.amount,
                "gesamtbetrag": t.total if (t := d.invoice_totals.get(x.bill_nr)) else None,
                "nr": x.bill_nr,
                "pdf": d.invoice_links.get(x.bill_nr),
            }
            for x in d.bills
        ],
    }


def _settlements(d: AccountData) -> list[dict[str, Any]]:
    """Settlement totals read from the invoice PDFs, oldest first."""
    rows = []
    for bill in d.bills:
        if (t := d.invoice_totals.get(bill.bill_nr)) is None:
            continue
        row = t.as_dict()
        # billed kWh per meter, keyed like the meter entities (tariff slug)
        row["kwh"] = {slugify(name): kwh for name, kwh in t.billed_kwh.items()}
        row["kwh_gesamt"] = round(sum(t.billed_kwh.values()), 1) if t.billed_kwh else None
        end = t.period_to or date(bill.ref_date.year if bill.ref_date.month > 6 else bill.ref_date.year - 1, 6, 30)
        start_year = t.period_from.year if t.period_from else end.year - 1
        row["jahr"] = f"{start_year}/{end.year % 100:02d}" if start_year != end.year else str(end.year)
        row["datum"] = bill.ref_date.isoformat()
        row["nr"] = bill.bill_nr
        row["pdf"] = d.invoice_links.get(bill.bill_nr)
        row["_end"] = end
        rows.append(row)
    rows.sort(key=lambda r: r.pop("_end"))
    return rows


def _settlement_attrs(d: AccountData, _today: date) -> dict[str, Any]:
    rows = _settlements(d)
    if not rows:
        return {}
    last = rows[-1]
    return {
        "jahr": last["jahr"],
        "zeitraum_von": last["von"],
        "zeitraum_bis": last["bis"],
        "zuschuss": last["zuschuss"],
        "kosten": last["kosten"],
        "strom_netto": last["strom_netto"],
        "sonstiges": last["sonstiges"],
        "teilbetraege": last["teilbetraege"],
        "saldo": last["saldo"],
        "pdf": last["pdf"],
        "abrechnungen": rows,
    }


def _instalment_attrs(d: AccountData, today: date) -> dict[str, Any]:
    nxt = d.next_instalment(today)
    if not nxt:
        return {}
    remaining = [i for i in d.instalments if i.due >= today]
    return {
        "faellig_am": nxt.due.isoformat(),
        "status": nxt.status,
        "offen": nxt.amount_open,
        "verbleibende_teilzahlungen": len(remaining),
        "summe_verbleibend": round(sum(i.amount for i in remaining), 2),
        "plan": [{"faellig_am": i.due.isoformat(), "betrag": i.amount, "status": i.status} for i in d.instalments],
    }


ACCOUNT_SENSORS: tuple[AccountSensorDescription, ...] = (
    AccountSensorDescription(
        key="naechste_teilzahlung",
        translation_key="naechste_teilzahlung",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="EUR",
        suggested_display_precision=2,
        icon="mdi:calendar-cash",
        value=lambda d, t: (n.amount if (n := d.next_instalment(t)) else None),
        attrs=_instalment_attrs,
    ),
    AccountSensorDescription(
        key="naechste_teilzahlung_am",
        translation_key="naechste_teilzahlung_am",
        device_class=SensorDeviceClass.DATE,
        icon="mdi:calendar-clock",
        value=lambda d, t: (n.due if (n := d.next_instalment(t)) else None),
    ),
    AccountSensorDescription(
        key="letzte_rechnung",
        translation_key="letzte_rechnung",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="EUR",
        suggested_display_precision=2,
        icon="mdi:file-document-outline",
        value=lambda d, _t: d.bills[0].amount if d.bills else None,
        attrs=_bill_attrs,
    ),
    AccountSensorDescription(
        key="abrechnung_gesamtkosten",
        translation_key="abrechnung_gesamtkosten",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="EUR",
        suggested_display_precision=2,
        icon="mdi:receipt-text-outline",
        value=lambda d, _t: (rows[-1]["brutto"] if (rows := _settlements(d)) else None),
        attrs=_settlement_attrs,
    ),
    AccountSensorDescription(
        key="letzte_rechnung_am",
        translation_key="letzte_rechnung_am",
        device_class=SensorDeviceClass.DATE,
        icon="mdi:file-clock-outline",
        value=lambda d, _t: d.bills[0].ref_date if d.bills else None,
    ),
    AccountSensorDescription(
        key="bonuspunkte",
        translation_key="bonuspunkte",
        native_unit_of_measurement="Punkte",
        icon="mdi:star-circle-outline",
        value=lambda d, _t: d.bonus_points,
        attrs=lambda d, _t: {"faktor": d.bonus_factor} if d.bonus_factor is not None else {},
    ),
)


class AccountSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    # Lists and invoice numbers stay out of the recorder database.
    _unrecorded_attributes = frozenset({"rechnungen", "plan", "rechnungsnummer", "pdf", "abrechnungen"})
    entity_description: AccountSensorDescription

    def __init__(self, entry: BurgenlandEnergieConfigEntry, description: AccountSensorDescription) -> None:
        super().__init__(entry.runtime_data.coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = _account_device(entry)
        self.entity_id = f"sensor.{DOMAIN}_{description.key}"

    @property
    def native_value(self) -> Any:
        data = self.coordinator.data
        return self.entity_description.value(data, dt_util.now().date()) if data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        return self.entity_description.attrs(data, dt_util.now().date()) if data else {}


# --------------------------------------------------------------- per meter


@dataclass(frozen=True, kw_only=True)
class TariffSensorDescription(SensorEntityDescription):
    value: Callable[[Tariff], Any]
    attrs: Callable[[Tariff], dict[str, Any]] = lambda _t: {}


def _tariff_attrs(t: Tariff) -> dict[str, Any]:
    return {
        "tarif": t.name,
        "tarifart": t.tariff_type,
        "netto": t.work_price_net,
        "brutto": t.work_price_gross,
        "preisgarantie_bis": t.price_guarantee_until.isoformat() if t.price_guarantee_until else None,
        "bindung_bis": t.bound_until.isoformat() if t.bound_until else None,
        "energiegemeinschaft_mitglied": t.community_member,
    }


TARIFF_SENSORS: tuple[TariffSensorDescription, ...] = (
    TariffSensorDescription(
        key="arbeitspreis",
        translation_key="arbeitspreis",
        native_unit_of_measurement="EUR/kWh",
        suggested_display_precision=4,
        icon="mdi:cash-multiple",
        value=lambda t: t.work_price_gross,
        attrs=_tariff_attrs,
    ),
    TariffSensorDescription(
        key="arbeitspreis_netto",
        translation_key="arbeitspreis_netto",
        native_unit_of_measurement="EUR/kWh",
        suggested_display_precision=4,
        icon="mdi:cash",
        value=lambda t: t.work_price_net,
    ),
    TariffSensorDescription(
        key="grundpreis",
        translation_key="grundpreis",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="EUR",
        suggested_display_precision=2,
        icon="mdi:calendar-month",
        value=lambda t: t.base_price_gross,
        attrs=lambda t: {
            "netto_pro_monat": t.base_price_net,
            "brutto_pro_jahr": round(t.base_price_gross * 12, 2) if t.base_price_gross is not None else None,
        },
    ),
)


class TariffSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    entity_description: TariffSensorDescription

    def __init__(
        self, entry: BurgenlandEnergieConfigEntry, tariff: Tariff, description: TariffSensorDescription
    ) -> None:
        super().__init__(entry.runtime_data.coordinator)
        self.entity_description = description
        self._meter_id = tariff.meter_id
        self._attr_unique_id = f"{entry.entry_id}_{tariff.meter_id}_{description.key}"
        self._attr_device_info = _meter_device(entry, tariff)
        self.entity_id = f"sensor.{DOMAIN}_{tariff.slug}_{description.key}"

    def _tariff(self) -> Tariff | None:
        data = self.coordinator.data
        return next((t for t in data.tariffs if t.meter_id == self._meter_id), None) if data else None

    @property
    def available(self) -> bool:
        return super().available and self._tariff() is not None

    @property
    def native_value(self) -> Any:
        t = self._tariff()
        return self.entity_description.value(t) if t else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        t = self._tariff()
        return self.entity_description.attrs(t) if t else {}


class MeterStatusSensor(SensorEntity):
    """Latest day with billed energy for one meter, plus import status."""

    _unrecorded_attributes = frozenset({"kwh_pro_monat", "kwh_pro_jahr"})
    _attr_has_entity_name = True
    _attr_translation_key = "letzter_messtag"
    _attr_device_class = SensorDeviceClass.DATE
    _attr_icon = "mdi:meter-electric-outline"
    _attr_should_poll = False

    def __init__(self, entry: BurgenlandEnergieConfigEntry, tariff: Tariff, importer: MeterImporter) -> None:
        self._tariff = tariff
        self._importer = importer
        self._attr_unique_id = f"{entry.entry_id}_{tariff.meter_id}_letzter_messtag"
        self._attr_device_info = _meter_device(entry, tariff)
        self.entity_id = f"sensor.{DOMAIN}_{tariff.slug}_letzter_messtag"

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_METER_UPDATED, self._updated))

    @callback
    def _updated(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> date | None:
        st = self._importer.status.get(self._tariff.meter_id)
        return st.last_day if st else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        st = self._importer.status.get(self._tariff.meter_id)
        attrs: dict[str, Any] = {"statistik_id": statistic_id(self._tariff)}
        if st:
            attrs.update(
                {
                    "kwh": st.last_day_kwh,
                    "kwh_pro_jahr": st.kwh_per_year,
                    "kwh_pro_monat": st.kwh_per_month,
                    "letzter_lauf": st.last_run.isoformat() if st.last_run else None,
                    "geschriebene_tage": st.days_written,
                    "fehler": st.last_error,
                }
            )
        return attrs


class DataStatusSensor(CoordinatorEntity, SensorEntity):
    """Is everything there? Invoices saved as PDFs, usage data current, no errors."""

    _attr_has_entity_name = True
    _attr_translation_key = "datenstand"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["vollstaendig", "unvollstaendig", "fehler"]
    _attr_icon = "mdi:database-check-outline"
    _unrecorded_attributes = frozenset({"rechnungen_fehlend", "hinweise"})

    def __init__(self, entry: BurgenlandEnergieConfigEntry) -> None:
        super().__init__(entry.runtime_data.coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_datenstand"
        self._attr_device_info = _account_device(entry)
        self.entity_id = f"sensor.{DOMAIN}_datenstand"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_METER_UPDATED, self.async_write_ha_state))

    @property
    def available(self) -> bool:
        return True  # reports a failed update itself

    def _evaluate(self) -> tuple[str, dict[str, Any]]:
        coordinator = self.coordinator
        data: AccountData | None = coordinator.data
        archive_on = bool(self._entry.options.get(CONF_SAVE_INVOICES))
        hints: list[str] = []
        errors: list[str] = []
        if not coordinator.last_update_success:
            errors.append(f"Abruf fehlgeschlagen: {coordinator.last_exception}")
        meters = self._entry.runtime_data.importer.status
        for meter_id, st in meters.items():
            if st.last_error:
                errors.append(f"Verbrauchsdaten {meter_id}: {st.last_error}")
        bills = data.bills if data else []
        saved = data.invoice_files if data else {}
        missing = [b for b in bills if b.bill_nr not in saved]
        if not archive_on:
            hints.append("PDF-Archiv ist ausgeschaltet (Optionen der Integration)")
        elif missing:
            hints.append(f"{len(missing)} von {len(bills)} Rechnungen noch nicht als PDF gespeichert")
        totals = data.invoice_totals if data else {}
        last_days = [st.last_day for st in meters.values() if st.last_day]
        data_until = min(last_days) if last_days else None
        age = (dt_util.now().date() - data_until).days if data_until else None
        if age is not None and age > METER_DATA_STALE_DAYS:
            hints.append(f"Verbrauchsdaten seit {age} Tagen nicht nachgeliefert")
        if errors:
            state = "fehler"
        elif hints:
            state = "unvollstaendig"
        else:
            state = "vollstaendig"
        attrs = {
            "rechnungen_gesamt": len(bills),
            "rechnungen_gespeichert": len([b for b in bills if b.bill_nr in saved]),
            # informational: not every invoice is a settlement with a cost summary
            "rechnungen_ausgewertet": len([b for b in bills if b.bill_nr in totals]),
            "rechnungen_fehlend": [{"datum": b.ref_date.isoformat(), "nr": b.bill_nr} for b in missing],
            "pdf_archiv": archive_on,
            "messdaten_bis": data_until.isoformat() if data_until else None,
            "messdaten_alter_tage": age,
            "letzter_abruf": coordinator.last_update_success_time.isoformat()
            if getattr(coordinator, "last_update_success_time", None)
            else None,
            "fehler": errors,
            "hinweise": hints,
        }
        return state, attrs

    @property
    def native_value(self) -> str:
        return self._evaluate()[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._evaluate()[1]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BurgenlandEnergieConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    rt = entry.runtime_data
    entities: list[SensorEntity] = [AccountSensor(entry, d) for d in ACCOUNT_SENSORS]
    entities.append(DataStatusSensor(entry))
    for tariff in rt.coordinator.data.tariffs:
        entities.extend(TariffSensor(entry, tariff, d) for d in TARIFF_SENSORS)
        entities.append(MeterStatusSensor(entry, tariff, rt.importer))
    async_add_entities(entities)
