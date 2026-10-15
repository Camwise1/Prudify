"""Set up Prudify from the UI: its address and API key."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_API_KEY, CONF_URL
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import PrudifyAuthError, PrudifyClient, PrudifyError
from .const import DOMAIN

SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL): str,
        vol.Required(CONF_API_KEY): str,
    }
)


class PrudifyConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def _check(self, url: str, api_key: str) -> tuple[dict[str, str], dict[str, Any]]:
        client = PrudifyClient(async_get_clientsession(self.hass), url, api_key)
        try:
            return {}, await client.summary()
        except PrudifyAuthError:
            return {"base": "invalid_auth"}, {}
        except PrudifyError:
            return {"base": "cannot_connect"}, {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input[CONF_URL].strip().rstrip("/")
            await self.async_set_unique_id(url.lower())
            self._abort_if_unique_id_configured()
            errors, summary = await self._check(url, user_input[CONF_API_KEY])
            if not errors:
                return self.async_create_entry(
                    title=f"Prudify ({url.split('://')[-1]})",
                    data={CONF_URL: url, CONF_API_KEY: user_input[CONF_API_KEY]},
                    description_placeholders={"version": summary.get("version", "")},
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(SCHEMA, user_input or {}),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The API key was regenerated in Prudify; ask for the new one."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors, _ = await self._check(entry.data[CONF_URL], user_input[CONF_API_KEY])
            if not errors:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_API_KEY: user_input[CONF_API_KEY]}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_API_KEY): str}),
            errors=errors,
        )
