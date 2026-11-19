"""Config flow for Narwal vacuum integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback

try:
    from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
except ImportError:  # HA < 2023.9 fallback
    from homeassistant.components.zeroconf import ZeroconfServiceInfo

from .const import (
    CONF_MAP_SCALE,
    CONF_MODEL,
    CONF_PRODUCT_KEY,
    DEFAULT_MAP_SCALE,
    DEFAULT_PORT,
    DOMAIN,
    MAX_MAP_SCALE,
    MIN_MAP_SCALE,
    NARWAL_MODELS,
)
from .narwal_client import NarwalClient

_LOGGER = logging.getLogger(__name__)

MODEL_OPTIONS = list(NARWAL_MODELS.keys())

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("host"): str,
        vol.Optional("port", default=DEFAULT_PORT): int,
        vol.Required(CONF_MODEL, default=MODEL_OPTIONS[0]): vol.In(MODEL_OPTIONS),
    }
)


class NarwalOptionsFlow(OptionsFlow):
    """Handle Narwal options (map rendering scale)."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_MAP_SCALE,
                    default=self._config_entry.options.get(
                        CONF_MAP_SCALE, DEFAULT_MAP_SCALE
                    ),
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_MAP_SCALE, max=MAX_MAP_SCALE),
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)


class NarwalConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Narwal vacuum."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovered: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> NarwalOptionsFlow:
        """Create the options flow."""
        return NarwalOptionsFlow(config_entry)

    async def _async_try_connect(
        self, host: str, port: int, model_label: str
    ) -> tuple[str, str]:
        """Connect to the robot and return (device_id, resolved_product_key).

        Raises on any connection/discovery failure.
        """
        product_key = NARWAL_MODELS[model_label]

        # If user selected a specific model, set topic prefix directly
        topic_prefix = None if product_key == "auto" else f"/{product_key}"

        client = NarwalClient(
            host=host, port=port, topic_prefix=topic_prefix,
        )
        try:
            await client.connect()
            # Discover device_id from broadcast, then query info
            await client.discover_device_id(timeout=15.0)
            # Drain any stale field5 responses left in the WebSocket
            # buffer from discover's wake probes before sending a
            # real command
            await client.drain_ws_buffer()
            device_info = await client.get_device_info()
        finally:
            await client.disconnect()

        # Use the product key that actually worked (may have been
        # auto-detected during discovery even if user picked "auto")
        resolved_key = client.topic_prefix.lstrip("/")
        return device_info.device_id, resolved_key

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step — user enters IP, port, and model."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input["host"]
            port = user_input.get("port", DEFAULT_PORT)
            model_label = user_input[CONF_MODEL]

            try:
                device_id, resolved_key = await self._async_try_connect(
                    host, port, model_label
                )
            except Exception as ex:
                _LOGGER.warning(
                    "Setup failed: %s: %s", type(ex).__name__, ex,
                )
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(device_id)
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=(
                        model_label
                        if NARWAL_MODELS[model_label] != "auto"
                        else f"Narwal {resolved_key}"
                    ),
                    data={
                        "host": host,
                        "port": port,
                        "device_id": device_id,
                        CONF_PRODUCT_KEY: resolved_key,
                        CONF_MODEL: model_label,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle mDNS discovery of a Narwal robot (_narwal_sweeper._tcp).

        The robot advertises this service on the LAN; it is how the official
        app finds devices for the local channel.
        """
        host = discovery_info.host
        port = discovery_info.port or DEFAULT_PORT

        # Log the full advertisement — TXT properties may carry the product
        # key / device id / protocol version needed to talk to the robot.
        _LOGGER.info(
            "Narwal mDNS discovery: host=%s port=%s name=%s "
            "hostname=%s properties=%s addresses=%s",
            host,
            port,
            discovery_info.name,
            getattr(discovery_info, "hostname", None),
            discovery_info.properties,
            getattr(discovery_info, "addresses", None),
        )

        # Skip if an entry already exists for this host.
        self._async_abort_entries_match({"host": host})

        self._discovered = {"host": host, "port": port}
        self.context["title_placeholders"] = {"name": f"Narwal {host}"}

        # NOTE: the robot accepts only ONE WebSocket client at a time — do
        # NOT probe it here. A lingering probe connection would make the
        # confirm step's own connect fail. Deduplication happens via the
        # host match above and the unique id assigned on confirm.
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm adding a robot discovered via mDNS."""
        errors: dict[str, str] = {}
        host = self._discovered["host"]
        port = self._discovered["port"]

        if user_input is not None:
            model_label = user_input[CONF_MODEL]
            try:
                device_id, resolved_key = await self._async_try_connect(
                    host, port, model_label
                )
            except Exception as ex:
                _LOGGER.warning(
                    "Zeroconf setup failed for %s: %s: %s",
                    host, type(ex).__name__, ex,
                )
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(device_id)
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=(
                        model_label
                        if NARWAL_MODELS[model_label] != "auto"
                        else f"Narwal {resolved_key}"
                    ),
                    data={
                        "host": host,
                        "port": port,
                        "device_id": device_id,
                        CONF_PRODUCT_KEY: resolved_key,
                        CONF_MODEL: model_label,
                    },
                )

        return self.async_show_form(
            step_id="zeroconf_confirm",
            data_schema=vol.Schema(
                {
                    # Discovered model is unknown — default to auto-detect,
                    # which cycles all product keys.
                    vol.Required(
                        CONF_MODEL, default=MODEL_OPTIONS[-1]
                    ): vol.In(MODEL_OPTIONS),
                }
            ),
            description_placeholders={"host": host},
            errors=errors,
        )
