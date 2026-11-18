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

from . import handover
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

    _reauth: bool = False
    _handover_token: str | None = None

    async def _finish(self, client: BurgenlandEnergieClient) -> ConfigFlowResult | str:
        """Create or update the entry; returns an error key if not possible."""
        if self._reauth:
            entry = self._get_reauth_entry()
            if client.customer_nr != entry.unique_id:
                return "wrong_account"
            return self.async_update_reload_and_abort(
                entry, data={**entry.data, CONF_REFRESH_TOKEN: client.refresh_token}
            )
        await self.async_set_unique_id(client.customer_nr, raise_on_progress=False)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="Burgenland Energie",
            data={CONF_REFRESH_TOKEN: client.refresh_token, CONF_CUSTOMER_NR: client.customer_nr},
        )

    # -- entry points: a menu with both ways of handing over the token

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=["handover", "paste"])

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._reauth = True
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="reauth_confirm", menu_options=["handover", "paste"])

    # -- one-click handover (bookmarklet -> one-time Home Assistant page)

    async def async_step_handover(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None:
            url = handover.async_create(self.hass, self.flow_id)
            return self.async_external_step(step_id="handover", url=url)
        # Called by the handover view with the token.
        self._handover_token = user_input[CONF_REFRESH_TOKEN]
        return self.async_external_step_done(next_step_id="handover_done")

    async def async_step_handover_done(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        token, self._handover_token = self._handover_token or "", None
        client, error = await self._validate(token)
        if client:
            result = await self._finish(client)
            if not isinstance(result, str):
                return result
            error = result
        # Fall back to pasting, with the reason shown.
        return self.async_show_form(
            step_id="paste",
            data_schema=TOKEN_SCHEMA,
            errors={"base": error or "unknown"},
            description_placeholders={"bookmarklet": BOOKMARKLET},
        )

    # -- paste the token manually

    async def async_step_paste(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            client, error = await self._validate(user_input[CONF_REFRESH_TOKEN])
            if client:
                result = await self._finish(client)
                if not isinstance(result, str):
                    return result
                error = result
            errors["base"] = error or "unknown"
        return self.async_show_form(
            step_id="paste",
            data_schema=TOKEN_SCHEMA,
            errors=errors,
            description_placeholders={"bookmarklet": BOOKMARKLET},
        )

    @callback
    def async_remove(self) -> None:
        handover.async_discard(self.hass, self.flow_id)

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
