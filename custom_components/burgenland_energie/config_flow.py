"""Config flow for Burgenland Energie."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    BurgenlandEnergieAuthError,
    BurgenlandEnergieClient,
    BurgenlandEnergieError,
)
from .const import (
    CONF_CUSTOMER_NR,
    CONF_METER_DAYS,
    CONF_REFRESH_TOKEN,
    CONF_SAVE_INVOICES,
    DEFAULT_METER_DAYS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

TOKEN_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_REFRESH_TOKEN): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        )
    }
)

# Bookmarklet shown in the dialog: copies the portal's refresh token.
BOOKMARKLET = (
    "javascript:navigator.clipboard.writeText("
    "localStorage.getItem('okc-auth-refreshToken')||'')"
    ".then(()=>alert('Token kopiert'))"
)


class BurgenlandEnergieConfigFlow(ConfigFlow, domain=DOMAIN):
    """Take over a portal session via its refresh token."""

    VERSION = 1

    async def _validate(self, token: str) -> tuple[BurgenlandEnergieClient | None, str | None]:
        token = token.strip()
        if len(token) < 100 or token.count(" "):
            return None, "invalid_token"
        client = BurgenlandEnergieClient(
            async_create_clientsession(self.hass, cookie_jar=aiohttp.DummyCookieJar()), token
        )
        try:
            await client.async_refresh()
            await client.async_get_contracts()
        except BurgenlandEnergieAuthError:
            return None, "invalid_auth"
        except BurgenlandEnergieError as err:
            _LOGGER.warning("Burgenland Energie portal check failed: %s", err)
            return None, "cannot_connect"
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error validating the token")
            return None, "unknown"
        return client, None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            client, error = await self._validate(user_input[CONF_REFRESH_TOKEN])
            if client:
                await self.async_set_unique_id(client.customer_nr, raise_on_progress=False)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Burgenland Energie",
                    data={
                        CONF_REFRESH_TOKEN: client.refresh_token,
                        CONF_CUSTOMER_NR: client.customer_nr,
                    },
                )
            errors["base"] = error or "unknown"
        return self.async_show_form(
            step_id="user",
            data_schema=TOKEN_SCHEMA,
            errors=errors,
            description_placeholders={"bookmarklet": BOOKMARKLET},
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            client, error = await self._validate(user_input[CONF_REFRESH_TOKEN])
            if client:
                if client.customer_nr != entry.unique_id:
                    errors["base"] = "wrong_account"
                else:
                    return self.async_update_reload_and_abort(
                        entry, data={**entry.data, CONF_REFRESH_TOKEN: client.refresh_token}
                    )
            else:
                errors["base"] = error or "unknown"
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=TOKEN_SCHEMA,
            errors=errors,
            description_placeholders={"bookmarklet": BOOKMARKLET},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return BurgenlandEnergieOptionsFlow()


class BurgenlandEnergieOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_SAVE_INVOICES: bool(user_input[CONF_SAVE_INVOICES]),
                    CONF_METER_DAYS: int(user_input[CONF_METER_DAYS]),
                }
            )
        opts = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SAVE_INVOICES, default=opts.get(CONF_SAVE_INVOICES, False)): BooleanSelector(),
                    vol.Required(CONF_METER_DAYS, default=opts.get(CONF_METER_DAYS, DEFAULT_METER_DAYS)): NumberSelector(
                        NumberSelectorConfig(min=7, max=120, step=1, mode=NumberSelectorMode.BOX)
                    ),
                }
            ),
        )
