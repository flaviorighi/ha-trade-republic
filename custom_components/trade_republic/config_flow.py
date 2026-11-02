"""Config flow for the Trade Republic integration."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging
import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.const import CONF_CODE, CONF_PIN, CONF_SCAN_INTERVAL
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import create_client
from .api import TooManyAttemptsError, TradeRepublicAuthError, TradeRepublicClient, TradeRepublicConnectionError
from .const import CONF_PHONE, DEFAULT_SCAN_INTERVAL, DOMAIN, MAX_SCAN_MINUTES, MIN_SCAN_MINUTES
from .coordinator import TradeRepublicConfigEntry

_LOGGER = logging.getLogger(__name__)

PHONE_RE = re.compile(r"^\+[1-9]\d{6,14}$")

PIN_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PHONE): TextSelector(TextSelectorConfig(type=TextSelectorType.TEL)),
        vol.Required(CONF_PIN): PIN_SELECTOR,
    }
)
REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PIN): PIN_SELECTOR})
CODE_SCHEMA = vol.Schema({vol.Required(CONF_CODE): str})


def normalize_phone(phone: str) -> str:
    """+39 333 123-4567 and 0039333... both become +393331234567."""
    phone = re.sub(r"[\s\-/().]", "", phone)
    if phone.startswith("00"):
        phone = "+" + phone[2:]
    return phone


class TradeRepublicConfigFlow(ConfigFlow, domain=DOMAIN):
    """Log in with phone number, PIN and a confirmation in the Trade Republic app."""

    VERSION = 1
    MINOR_VERSION = 2

    def __init__(self) -> None:
        self._client: TradeRepublicClient | None = None
        self._needs_code = False
        self._confirm_task: asyncio.Task[str | None] | None = None
        # The step that asks for the PIN, where a failed confirmation sends the user back.
        self._login_step = "user"
        self._error: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: TradeRepublicConfigEntry) -> TradeRepublicOptionsFlow:
        return TradeRepublicOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors = self._take_error()
        if user_input is not None:
            phone = normalize_phone(user_input[CONF_PHONE])
            if not PHONE_RE.match(phone):
                errors[CONF_PHONE] = "invalid_phone"
            else:
                self._async_abort_entries_match({CONF_PHONE: phone})
                if (error := await self._async_start_login(phone, user_input[CONF_PIN])) is None:
                    return await self._async_step_second_factor()
                errors["base"] = error
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                USER_SCHEMA, {CONF_PHONE: user_input[CONF_PHONE]} if user_input else {}
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._login_step = "reauth_confirm"
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        phone = self._get_reauth_entry().data[CONF_PHONE]
        errors = self._take_error()
        if user_input is not None:
            if (error := await self._async_start_login(phone, user_input[CONF_PIN])) is None:
                return await self._async_step_second_factor()
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={"phone": phone},
        )

    async def async_step_authenticator(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._client is not None
        if user_input is None:
            return self.async_show_form(step_id="authenticator", data_schema=CODE_SCHEMA)
        try:
            await self.hass.async_add_executor_job(self._client.complete_with_code, user_input[CONF_CODE])
        except TradeRepublicAuthError:
            self._error = "invalid_code"
        except TradeRepublicConnectionError:
            self._error = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error completing the login")
            self._error = "unknown"
        if self._error:
            # Trade Republic closes the login after a wrong code: start again from the PIN.
            return await getattr(self, f"async_step_{self._login_step}")()
        return await self.async_step_finish()

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._confirm_task is None:
            # Not started eagerly: a wait that ended at once would answer this same call with
            # "progress done", and Home Assistant would pass the PIN form's input on to the
            # next step, starting the login over.
            self._confirm_task = self.hass.async_create_task(self._async_wait_for_confirmation(), eager_start=False)
        if not self._confirm_task.done():
            return self.async_show_progress(
                step_id="confirm", progress_action="confirm_in_app", progress_task=self._confirm_task
            )
        self._error = self._confirm_task.result()
        self._confirm_task = None
        return self.async_show_progress_done(next_step_id=self._login_step if self._error else "finish")

    async def async_step_finish(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._client is not None
        try:
            account_id = await self.hass.async_add_executor_job(self._client.account_id)
        except (TradeRepublicAuthError, TradeRepublicConnectionError):
            return self.async_abort(reason="cannot_connect")
        await self.async_set_unique_id(account_id)
        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(self._get_reauth_entry())
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="Trade Republic", data={CONF_PHONE: self._client.phone})

    async def _async_start_login(self, phone: str, pin: str) -> str | None:
        """Send phone number and PIN; returns an error key, or None when the login started."""
        client = create_client(self.hass, phone)
        try:
            self._needs_code = await self.hass.async_add_executor_job(client.start_login, pin)
        except TooManyAttemptsError:
            return "too_many_attempts"
        except TradeRepublicAuthError:
            return "invalid_auth"
        except TradeRepublicConnectionError:
            return "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error starting the login")
            return "unknown"
        self._client = client
        return None

    async def _async_step_second_factor(self) -> ConfigFlowResult:
        if self._needs_code:
            return await self.async_step_authenticator()
        return await self.async_step_confirm()

    async def _async_wait_for_confirmation(self) -> str | None:
        assert self._client is not None
        try:
            await self.hass.async_add_executor_job(self._client.wait_for_confirmation)
        except TradeRepublicAuthError:
            return "not_confirmed"
        except TradeRepublicConnectionError:
            return "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error waiting for the confirmation")
            return "unknown"
        return None

    def _take_error(self) -> dict[str, str]:
        error, self._error = self._error, None
        return {"base": error} if error else {}


class TradeRepublicOptionsFlow(OptionsFlowWithReload):
    """How often to read the portfolio."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data={CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])})
        schema = vol.Schema(
            {
                vol.Required(CONF_SCAN_INTERVAL): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_MINUTES,
                        max=MAX_SCAN_MINUTES,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="min",
                    )
                )
            }
        )
        current = self.config_entry.options.get(CONF_SCAN_INTERVAL, int(DEFAULT_SCAN_INTERVAL.total_seconds() // 60))
        return self.async_show_form(
            step_id="init", data_schema=self.add_suggested_values_to_schema(schema, {CONF_SCAN_INTERVAL: current})
        )
