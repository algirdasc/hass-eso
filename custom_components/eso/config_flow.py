"""Config flow for the ESO Energy Consumption integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryData,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import selector

from .const import (
    CONF_CONSUMED,
    CONF_EXPORT_BALANCE,
    CONF_FIXED_PRICE,
    CONF_IMAP,
    CONF_IMAP_FOLDER,
    CONF_IMAP_HOST,
    CONF_IMAP_PORT,
    CONF_IMAP_SENDER,
    CONF_OBJECTS,
    CONF_PRICE_CURRENCY,
    CONF_PRICE_ENTITY,
    CONF_PROVIDER,
    CONF_RETURNED,
    DEFAULT_IMAP_FOLDER,
    DEFAULT_IMAP_HOST,
    DEFAULT_IMAP_PORT,
    DEFAULT_IMAP_SENDER,
    DEFAULT_PRICE_CURRENCY,
    DEFAULT_PROVIDER,
    DOMAIN,
    PROVIDER_ESO,
    PROVIDER_IGNITIS,
    PROVIDERS,
    SESSION_FILE,
    SUBENTRY_TYPE_OBJECT,
)
from .eso_client import (
    ESOAuthError,
    ESOClient,
    ESOConnectionError,
    ESOError,
)
from .ignitis_client import IgnitisClient

_LOGGER = logging.getLogger(__name__)

CONF_ID = "id"
CONF_NAME = "name"
CONF_SELECTED = "selected"
CONF_OBJECT = "object"
# Distinct form-field keys for the mailbox so they never collide with the ESO
# account username/password when shown on the same form.
CONF_IMAP_USERNAME = "imap_username"
CONF_IMAP_PASSWORD = "imap_password"


def _imap_schema(defaults: dict | None = None) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_IMAP_USERNAME, default=defaults.get(CONF_IMAP_USERNAME, "")
            ): str,
            vol.Required(
                CONF_IMAP_PASSWORD, default=defaults.get(CONF_IMAP_PASSWORD, "")
            ): str,
            vol.Required(
                CONF_IMAP_HOST, default=defaults.get(CONF_IMAP_HOST, DEFAULT_IMAP_HOST)
            ): str,
            vol.Required(
                CONF_IMAP_PORT, default=defaults.get(CONF_IMAP_PORT, DEFAULT_IMAP_PORT)
            ): cv.port,
            vol.Required(
                CONF_IMAP_SENDER,
                default=defaults.get(CONF_IMAP_SENDER, DEFAULT_IMAP_SENDER),
            ): str,
            vol.Required(
                CONF_IMAP_FOLDER,
                default=defaults.get(CONF_IMAP_FOLDER, DEFAULT_IMAP_FOLDER),
            ): str,
        }
    )


def _build_imap_config(user_input: dict) -> dict:
    """Turn IMAP form input into a stored config block (mailbox is required)."""
    return {
        CONF_USERNAME: user_input[CONF_IMAP_USERNAME],
        CONF_PASSWORD: user_input[CONF_IMAP_PASSWORD],
        CONF_IMAP_HOST: user_input.get(CONF_IMAP_HOST, DEFAULT_IMAP_HOST),
        CONF_IMAP_PORT: user_input.get(CONF_IMAP_PORT, DEFAULT_IMAP_PORT),
        CONF_IMAP_SENDER: user_input.get(CONF_IMAP_SENDER, DEFAULT_IMAP_SENDER),
        CONF_IMAP_FOLDER: user_input.get(CONF_IMAP_FOLDER, DEFAULT_IMAP_FOLDER),
    }


def _runtime_imap(imap: dict | None) -> dict | None:
    """Map a stored IMAP block to the keyword shape ESOClient expects."""
    if not imap:
        return None
    return {
        "host": imap[CONF_IMAP_HOST],
        "port": imap[CONF_IMAP_PORT],
        "username": imap[CONF_USERNAME],
        "password": imap[CONF_PASSWORD],
        "sender": imap[CONF_IMAP_SENDER],
        "folder": imap[CONF_IMAP_FOLDER],
    }


def _provider_selector() -> selector.SelectSelector:
    """A translatable dropdown of the available data providers."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=PROVIDERS,
            translation_key=CONF_PROVIDER,
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def _make_client(
    hass, provider: str, username: str, password: str, imap: dict | None = None
) -> ESOClient | IgnitisClient:
    """Build the data-provider client for validation and object discovery."""
    if provider == PROVIDER_IGNITIS:
        return IgnitisClient(username=username, password=password)
    return ESOClient(
        username=username,
        password=password,
        imap_config=_runtime_imap(imap),
        session_file=hass.config.path(SESSION_FILE),
    )


def _unique_id(provider: str, username: str) -> str:
    """Namespace the unique id per provider so the same email can be used on
    both providers. ESO keeps the bare username for backward compatibility."""
    if provider == PROVIDER_ESO:
        return username.lower()
    return f"{provider}:{username.lower()}"


def _settings_schema(defaults: dict) -> vol.Schema:
    """Schema for a single object's settings (used for add & reconfigure)."""
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "")): str,
            vol.Required(
                CONF_CONSUMED, default=defaults.get(CONF_CONSUMED, True)
            ): bool,
            vol.Required(
                CONF_RETURNED, default=defaults.get(CONF_RETURNED, False)
            ): bool,
            vol.Optional(
                CONF_PRICE_ENTITY,
                description={"suggested_value": defaults.get(CONF_PRICE_ENTITY)},
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            vol.Required(
                CONF_PRICE_CURRENCY,
                default=defaults.get(CONF_PRICE_CURRENCY, DEFAULT_PRICE_CURRENCY),
            ): str,
            vol.Optional(
                CONF_FIXED_PRICE,
                description={"suggested_value": defaults.get(CONF_FIXED_PRICE)},
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(mode="box", step="any")
            ),
            vol.Required(
                CONF_EXPORT_BALANCE, default=defaults.get(CONF_EXPORT_BALANCE, False)
            ): bool,
        }
    )


def _object_from_settings(obj_id: str, name: str, user_input: dict) -> dict:
    """Build a stored object dict from a settings form submission."""
    obj = {
        CONF_ID: str(obj_id),
        CONF_NAME: name,
        CONF_CONSUMED: user_input[CONF_CONSUMED],
        CONF_RETURNED: user_input[CONF_RETURNED],
        CONF_PRICE_CURRENCY: user_input[CONF_PRICE_CURRENCY],
        CONF_EXPORT_BALANCE: user_input.get(CONF_EXPORT_BALANCE, False),
    }
    price_entity = user_input.get(CONF_PRICE_ENTITY)
    if price_entity:
        obj[CONF_PRICE_ENTITY] = price_entity
    fixed_price = user_input.get(CONF_FIXED_PRICE)
    if fixed_price is not None:
        obj[CONF_FIXED_PRICE] = fixed_price
    return obj


def _object_subentry(obj: dict) -> ConfigSubentryData:
    """Wrap an object dict as an object subentry."""
    return ConfigSubentryData(
        data=obj,
        subentry_type=SUBENTRY_TYPE_OBJECT,
        title=obj[CONF_NAME],
        unique_id=str(obj[CONF_ID]),
    )


class ESOConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for ESO Energy Consumption."""

    VERSION = 1

    def __init__(self) -> None:
        self._provider: str = DEFAULT_PROVIDER
        self._username: str | None = None
        self._password: str | None = None
        self._imap: dict | None = None
        self._discovered: list[dict] = []
        self._reauth_entry: ConfigEntry | None = None

    # ---- step 1: provider + credentials -----------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._provider = user_input[CONF_PROVIDER]
            self._username = user_input[CONF_USERNAME]
            self._password = user_input[CONF_PASSWORD]
            await self.async_set_unique_id(
                _unique_id(self._provider, self._username)
            )
            self._abort_if_unique_id_configured()
            client = _make_client(
                self.hass, self._provider, self._username, self._password
            )
            try:
                valid = await self.hass.async_add_executor_job(client.check_password)
            except ESOConnectionError:
                errors["base"] = "cannot_connect"
            except ESOError:
                errors["base"] = "unknown"
            else:
                if valid:
                    if self._provider == PROVIDER_IGNITIS:
                        return await self.async_step_objects()
                    return await self.async_step_imap()
                errors["base"] = "invalid_auth"

        schema = vol.Schema(
            {
                vol.Required(CONF_PROVIDER, default=DEFAULT_PROVIDER): _provider_selector(),
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    # ---- step 2: IMAP / two-factor ----------------------------------------

    async def async_step_imap(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get(CONF_IMAP_USERNAME) or not user_input.get(
                CONF_IMAP_PASSWORD
            ):
                errors["base"] = "imap_required"
            else:
                self._imap = _build_imap_config(user_input)
                return await self.async_step_objects()
        return self.async_show_form(
            step_id="imap", data_schema=_imap_schema(), errors=errors
        )

    # ---- step 3: discovered objects ---------------------------------------

    async def async_step_objects(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if not self._discovered:
            client = _make_client(
                self.hass,
                self._provider,
                self._username,
                self._password,
                self._imap,
            )
            try:
                self._discovered = await self.hass.async_add_executor_job(
                    client.discover_objects
                )
            except ESOConnectionError:
                errors["base"] = "cannot_connect"
            except ESOAuthError:
                errors["base"] = "twofa_failed"
            except ESOError:
                errors["base"] = "unknown"
            if errors:
                # Let the user revisit the IMAP step and retry.
                if self._provider == PROVIDER_IGNITIS:
                    return self.async_show_form(
                        step_id="user",
                        data_schema=vol.Schema(
                            {
                                vol.Required(CONF_PROVIDER, default=self._provider): _provider_selector(),
                                vol.Required(CONF_USERNAME, default=self._username): str,
                                vol.Required(CONF_PASSWORD): str,
                            }
                        ),
                        errors=errors,
                    )
                return self.async_show_form(
                    step_id="imap", data_schema=_imap_schema(), errors=errors
                )
            if not self._discovered:
                return self.async_abort(reason="no_objects")

        choices = {obj[CONF_ID]: obj[CONF_NAME] for obj in self._discovered}

        if user_input is not None:
            selected = user_input[CONF_SELECTED]
            if not selected:
                errors["base"] = "no_objects_selected"
            else:
                subentries = [
                    _object_subentry(
                        {
                            CONF_ID: obj[CONF_ID],
                            CONF_NAME: obj[CONF_NAME],
                            CONF_CONSUMED: True,
                            CONF_RETURNED: False,
                            CONF_PRICE_CURRENCY: DEFAULT_PRICE_CURRENCY,
                        }
                    )
                    for obj in self._discovered
                    if obj[CONF_ID] in selected
                ]
                data: dict[str, Any] = {
                    CONF_PROVIDER: self._provider,
                    CONF_USERNAME: self._username,
                    CONF_PASSWORD: self._password,
                }
                if self._provider == PROVIDER_ESO:
                    data[CONF_IMAP] = self._imap
                return self.async_create_entry(
                    title=self._username, data=data, subentries=subentries
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_SELECTED, default=list(choices)): cv.multi_select(
                    choices
                )
            }
        )
        return self.async_show_form(
            step_id="objects", data_schema=schema, errors=errors
        )

    # ---- YAML import ------------------------------------------------------

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        username = import_data[CONF_USERNAME]
        provider = import_data.get(CONF_PROVIDER, DEFAULT_PROVIDER)
        await self.async_set_unique_id(_unique_id(provider, username))
        self._abort_if_unique_id_configured()

        data: dict[str, Any] = {
            CONF_PROVIDER: provider,
            CONF_USERNAME: username,
            CONF_PASSWORD: import_data[CONF_PASSWORD],
        }
        imap = import_data.get(CONF_IMAP)
        if imap and provider == PROVIDER_ESO:
            data[CONF_IMAP] = {
                CONF_USERNAME: imap[CONF_USERNAME],
                CONF_PASSWORD: imap[CONF_PASSWORD],
                CONF_IMAP_HOST: imap.get(CONF_IMAP_HOST, DEFAULT_IMAP_HOST),
                CONF_IMAP_PORT: imap.get(CONF_IMAP_PORT, DEFAULT_IMAP_PORT),
                CONF_IMAP_SENDER: imap.get(CONF_IMAP_SENDER, DEFAULT_IMAP_SENDER),
                CONF_IMAP_FOLDER: imap.get(CONF_IMAP_FOLDER, DEFAULT_IMAP_FOLDER),
            }

        subentries = []
        for obj in import_data.get(CONF_OBJECTS, []):
            entry = {
                CONF_ID: str(obj[CONF_ID]),
                CONF_NAME: obj[CONF_NAME],
                CONF_CONSUMED: obj.get(CONF_CONSUMED, True),
                CONF_RETURNED: obj.get(CONF_RETURNED, False),
                CONF_PRICE_CURRENCY: obj.get(
                    CONF_PRICE_CURRENCY, DEFAULT_PRICE_CURRENCY
                ),
                CONF_EXPORT_BALANCE: obj.get(CONF_EXPORT_BALANCE, False),
            }
            if obj.get(CONF_PRICE_ENTITY):
                entry[CONF_PRICE_ENTITY] = obj[CONF_PRICE_ENTITY]
            if obj.get(CONF_FIXED_PRICE) is not None:
                entry[CONF_FIXED_PRICE] = obj[CONF_FIXED_PRICE]
            subentries.append(_object_subentry(entry))

        return self.async_create_entry(
            title=username, data=data, subentries=subentries
        )

    # ---- reauth (e.g. missing mailbox after a YAML import) -----------------

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._reauth_entry
        assert entry is not None
        username = entry.data[CONF_USERNAME]
        imap = entry.data.get(CONF_IMAP) or {}

        if user_input is not None:
            password = user_input[CONF_PASSWORD]
            if not user_input.get(CONF_IMAP_USERNAME) or not user_input.get(
                CONF_IMAP_PASSWORD
            ):
                errors["base"] = "imap_required"
            else:
                client = ESOClient(username=username, password=password)
                try:
                    valid = await self.hass.async_add_executor_job(
                        client.check_password
                    )
                except ESOConnectionError:
                    errors["base"] = "cannot_connect"
                except ESOError:
                    errors["base"] = "unknown"
                else:
                    if not valid:
                        errors["base"] = "invalid_auth"
            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    data={
                        **entry.data,
                        CONF_PASSWORD: password,
                        CONF_IMAP: _build_imap_config(user_input),
                    },
                )

        schema = vol.Schema(
            {vol.Required(CONF_PASSWORD, default=entry.data.get(CONF_PASSWORD)): str}
        ).extend(
            _imap_schema(
                {
                    CONF_IMAP_USERNAME: imap.get(CONF_USERNAME, ""),
                    CONF_IMAP_PASSWORD: imap.get(CONF_PASSWORD, ""),
                    CONF_IMAP_HOST: imap.get(CONF_IMAP_HOST, DEFAULT_IMAP_HOST),
                    CONF_IMAP_PORT: imap.get(CONF_IMAP_PORT, DEFAULT_IMAP_PORT),
                    CONF_IMAP_SENDER: imap.get(CONF_IMAP_SENDER, DEFAULT_IMAP_SENDER),
                    CONF_IMAP_FOLDER: imap.get(CONF_IMAP_FOLDER, DEFAULT_IMAP_FOLDER),
                }
            ).schema
        )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=schema,
            errors=errors,
            description_placeholders={"username": username},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> ESOOptionsFlow:
        return ESOOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_TYPE_OBJECT: ESOObjectSubentryFlow}


class ESOOptionsFlow(OptionsFlow):
    """Account-level options: update ESO password and mailbox (2FA) settings."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        data = self.config_entry.data
        provider = data.get(CONF_PROVIDER, DEFAULT_PROVIDER)
        imap = data.get(CONF_IMAP) or {}
        is_eso = provider == PROVIDER_ESO

        if user_input is not None:
            username = data[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]
            if is_eso and (not user_input.get(CONF_IMAP_USERNAME) or not user_input.get(CONF_IMAP_PASSWORD)):
                errors["base"] = "imap_required"
            else:
                client = _make_client(self.hass, provider, username, password)
                try:
                    valid = await self.hass.async_add_executor_job(
                        client.check_password
                    )
                except ESOConnectionError:
                    errors["base"] = "cannot_connect"
                except ESOError:
                    errors["base"] = "unknown"
                else:
                    if not valid:
                        errors["base"] = "invalid_auth"
            if not errors:
                new_data: dict[str, Any] = {
                    CONF_PROVIDER: provider,
                    CONF_USERNAME: username,
                    CONF_PASSWORD: password,
                }
                if is_eso:
                    new_data[CONF_IMAP] = _build_imap_config(user_input)
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=new_data
                )
                return self.async_create_entry(data={})

        schema = vol.Schema(
            {vol.Required(CONF_PASSWORD, default=data.get(CONF_PASSWORD)): str}
        )
        if is_eso:
            schema = schema.extend(
                _imap_schema(
                    {
                        CONF_IMAP_USERNAME: imap.get(CONF_USERNAME, ""),
                        CONF_IMAP_PASSWORD: imap.get(CONF_PASSWORD, ""),
                        CONF_IMAP_HOST: imap.get(CONF_IMAP_HOST, DEFAULT_IMAP_HOST),
                        CONF_IMAP_PORT: imap.get(CONF_IMAP_PORT, DEFAULT_IMAP_PORT),
                        CONF_IMAP_SENDER: imap.get(CONF_IMAP_SENDER, DEFAULT_IMAP_SENDER),
                        CONF_IMAP_FOLDER: imap.get(CONF_IMAP_FOLDER, DEFAULT_IMAP_FOLDER),
                    }
                ).schema
            )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
            description_placeholders={"username": data.get(CONF_USERNAME)},
        )


class ESOObjectSubentryFlow(ConfigSubentryFlow):
    """Add or reconfigure a single ESO object (metering point)."""

    def __init__(self) -> None:
        self._discovered: list[dict] = []
        self._selected_id: str | None = None
        self._selected_name: str | None = None

    # ---- add an object -----------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        entry = self._get_entry()

        if not self._discovered:
            client = _make_client(
                self.hass,
                entry.data.get(CONF_PROVIDER, DEFAULT_PROVIDER),
                entry.data[CONF_USERNAME],
                entry.data[CONF_PASSWORD],
                entry.data.get(CONF_IMAP),
            )
            try:
                self._discovered = await self.hass.async_add_executor_job(
                    client.discover_objects
                )
            except ESOConnectionError:
                return self.async_abort(reason="cannot_connect")
            except ESOAuthError:
                return self.async_abort(reason="twofa_failed")
            except ESOError:
                return self.async_abort(reason="unknown")

        configured = {
            sub.unique_id
            for sub in entry.subentries.values()
            if sub.subentry_type == SUBENTRY_TYPE_OBJECT
        }
        addable = {
            obj[CONF_ID]: obj[CONF_NAME]
            for obj in self._discovered
            if obj[CONF_ID] not in configured
        }
        if not addable:
            return self.async_abort(reason="all_configured")

        if user_input is not None:
            self._selected_id = user_input[CONF_OBJECT]
            self._selected_name = addable[self._selected_id]
            return await self.async_step_settings()

        schema = vol.Schema({vol.Required(CONF_OBJECT): vol.In(addable)})
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        assert self._selected_id is not None
        if user_input is not None:
            obj = _object_from_settings(
                self._selected_id, user_input[CONF_NAME], user_input
            )
            return self.async_create_entry(
                title=obj[CONF_NAME], data=obj, unique_id=str(self._selected_id)
            )

        schema = _settings_schema({CONF_NAME: self._selected_name})
        return self.async_show_form(
            step_id="settings",
            data_schema=schema,
            description_placeholders={"name": self._selected_name},
        )

    # ---- reconfigure an existing object ------------------------------------

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        entry = self._get_entry()
        subentry = self._get_reconfigure_subentry()
        current = dict(subentry.data)

        if user_input is not None:
            obj = _object_from_settings(
                current[CONF_ID], user_input[CONF_NAME], user_input
            )
            return self.async_update_and_abort(
                entry, subentry, title=obj[CONF_NAME], data=obj
            )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_settings_schema(current),
            description_placeholders={"name": current.get(CONF_NAME, "")},
        )
