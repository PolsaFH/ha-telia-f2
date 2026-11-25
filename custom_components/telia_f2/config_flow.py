"""Config flow for the Telia F2 router integration."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    TeliaF2ApiError,
    TeliaF2AuthError,
    TeliaF2Client,
    TeliaF2ConnectionError,
    TeliaF2RateLimitError,
)
from .const import DEFAULT_HOST, DEFAULT_USERNAME, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
        vol.Required(CONF_USERNAME, default=DEFAULT_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class TeliaF2ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Telia F2."""

    VERSION = 1

    async def _async_try_login(self, data: Mapping[str, Any]) -> dict[str, str]:
        """Log in to the router and return form errors, if any."""
        client = TeliaF2Client(
            async_get_clientsession(self.hass),
            data[CONF_HOST],
            data[CONF_USERNAME],
            data[CONF_PASSWORD],
        )
        try:
            await client.async_login()
        except TeliaF2AuthError:
            return {"base": "invalid_auth"}
        except TeliaF2RateLimitError:
            return {"base": "too_many_attempts"}
        except TeliaF2ConnectionError as err:
            _LOGGER.debug("Could not connect to the router: %s", err)
            return {"base": "cannot_connect"}
        except TeliaF2ApiError as err:
            _LOGGER.warning("Unexpected login response from the router: %s", err)
            return {"base": "unexpected_response"}
        except Exception:
            _LOGGER.exception("Unexpected error while logging in to the router")
            return {"base": "unknown"}
        finally:
            await client.async_close()
        return {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_HOST])
            self._abort_if_unique_id_configured()
            errors = await self._async_try_login(user_input)
            if not errors:
                return self.async_create_entry(
                    title=f"Telia F2 ({user_input[CONF_HOST]})",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_DATA_SCHEMA,
                {k: v for k, v in (user_input or {}).items() if k != CONF_PASSWORD},
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Start when the router rejects the stored username or password."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Ask for the username and password again."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            errors = await self._async_try_login({**entry.data, **user_input})
            if not errors:
                return self.async_update_reload_and_abort(
                    entry, data_updates=user_input
                )

        username = (user_input or entry.data)[CONF_USERNAME]
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME, default=username): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            description_placeholders={"host": entry.data[CONF_HOST]},
            errors=errors,
        )
