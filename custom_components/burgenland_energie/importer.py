"""Import billed daily energy per meter into long-term statistics."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .api import BurgenlandEnergieAuthError, BurgenlandEnergieClient, Tariff
from .const import DOMAIN, SIGNAL_METER_UPDATED

try:  # HA 2025.10+
    from homeassistant.components.recorder.models import StatisticMeanType
except ImportError:  # pragma: no cover - older HA
    StatisticMeanType = None  # type: ignore[assignment]

_LOGGER = logging.getLogger(__name__)


@dataclass
class MeterStatus:
    """Outcome of the last import for one meter (shown on its sensor)."""

    last_day: date | None = None  # latest day with a non-zero value
    last_day_kwh: float | None = None
    last_run: datetime | None = None
    last_error: str | None = None
    days_written: int = 0
    totals: dict[int, float] = field(default_factory=dict)  # year -> kWh in window


def statistic_id(tariff: Tariff) -> str:
    return f"{DOMAIN}:{tariff.meter_id}_bezug"


def metadata(tariff: Tariff) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "source": DOMAIN,
        "statistic_id": statistic_id(tariff),
        "name": f"Burgenland Energie {tariff.name} · Bezug",
        "unit_of_measurement": "kWh",
        "has_sum": True,
        "unit_class": "energy",
    }
    if StatisticMeanType is not None:
        meta["mean_type"] = StatisticMeanType.NONE
    else:  # pragma: no cover
        meta["has_mean"] = False
    return meta


def local_midnight_utc(day: date) -> datetime:
    tz = dt_util.get_default_time_zone()
    return datetime.combine(day, time(0), tzinfo=tz).astimezone(timezone.utc)


def build_rows(days: list[tuple[date, float]], base_sum: float) -> list[dict[str, Any]]:
    """One row per day at local midnight; the day's kWh is the change."""
    total = base_sum
    rows = []
    for day, kwh in sorted(days):
        total += kwh
        rows.append({"start": local_midnight_utc(day), "sum": round(total, 4), "state": round(kwh, 4)})
    return rows


class MeterImporter:
    """Re-imports the last N days of every meter on each run.

    The supplier only shows energy it has billed (grid energy; energy from an
    energy community is not included) and delivers it with a delay, so recent
    days are re-read on every run and later corrections overwrite earlier
    values. Cumulative sums continue from the last row before the window.
    """

    def __init__(self, hass: HomeAssistant, client: BurgenlandEnergieClient) -> None:
        self.hass = hass
        self.client = client
        self.status: dict[str, MeterStatus] = {}

    async def _base_sum(self, stat_id: str, before: datetime) -> float:
        for lookback in (timedelta(days=70), timedelta(days=4000)):
            result = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                before - lookback,
                before,
                {stat_id},
                "hour",
                None,
                {"sum"},
            )
            rows = result.get(stat_id) or []
            if rows:
                return float(rows[-1].get("sum") or 0.0)
        return 0.0

    async def async_import(self, tariffs: list[Tariff], days_back: int) -> list[str]:
        """Import [yesterday - days_back + 1, yesterday]; returns failed meters."""
        today = dt_util.now().date()
        last = today - timedelta(days=1)
        first = last - timedelta(days=max(1, days_back) - 1)
        failed: list[str] = []
        for tariff in tariffs:
            st = self.status.setdefault(tariff.meter_id, MeterStatus())
            st.last_run = dt_util.utcnow()
            try:
                days = await self.client.async_get_meter_days(tariff, first, last)
            except BurgenlandEnergieAuthError:
                raise
            except Exception as err:  # noqa: BLE001 - keep other meters going
                _LOGGER.warning("Meter data for %s failed: %s", tariff.name, err)
                st.last_error = str(err)
                failed.append(tariff.meter_id)
                continue
            if not days:
                st.last_error = None
                st.days_written = 0
                continue
            stat_id = statistic_id(tariff)
            base = await self._base_sum(stat_id, local_midnight_utc(days[0][0]))
            rows = build_rows(days, base)
            async_add_external_statistics(self.hass, metadata(tariff), rows)
            nonzero = [d for d in days if d[1] > 0]
            if nonzero:
                st.last_day, st.last_day_kwh = nonzero[-1]
            st.totals = {}
            for d, kwh in days:
                st.totals[d.year] = round(st.totals.get(d.year, 0.0) + kwh, 3)
            st.days_written = len(rows)
            st.last_error = None
        async_dispatcher_send(self.hass, SIGNAL_METER_UPDATED)
        return failed
