"""Calendar of part-payment due dates (Teilzahlungen)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import BurgenlandEnergieConfigEntry
from .api import Instalment
from .const import DOMAIN
from .sensor import _account_device


def _event(i: Instalment) -> CalendarEvent:
    amount = f"{i.amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return CalendarEvent(
        start=i.due,
        end=i.due + timedelta(days=1),
        summary=f"Teilzahlung Burgenland Energie {amount} €",
        description=f"Status laut Portal: {i.status}" if i.status else None,
        uid=f"{DOMAIN}-teilzahlung-{i.due.isoformat()}",
    )


class InstalmentCalendar(CoordinatorEntity, CalendarEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "teilzahlungen"
    _attr_icon = "mdi:calendar-cash"

    def __init__(self, entry: BurgenlandEnergieConfigEntry) -> None:
        super().__init__(entry.runtime_data.coordinator)
        self._attr_unique_id = f"{entry.entry_id}_teilzahlungen"
        self._attr_device_info = _account_device(entry)
        self.entity_id = f"calendar.{DOMAIN}_teilzahlungen"

    def _instalments(self) -> list[Instalment]:
        data = self.coordinator.data
        return data.instalments if data else []

    @property
    def event(self) -> CalendarEvent | None:
        today = dt_util.now().date()
        nxt = next((i for i in self._instalments() if i.due >= today), None)
        return _event(nxt) if nxt else None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        first: date = dt_util.as_local(start_date).date()
        last: date = dt_util.as_local(end_date).date()
        return [_event(i) for i in self._instalments() if first <= i.due <= last]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BurgenlandEnergieConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([InstalmentCalendar(entry)])
