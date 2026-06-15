"""Config flow for Nanoleaf Panels."""

from __future__ import annotations

from homeassistant import config_entries

from . import DOMAIN


class NanoleafPanelsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Nanoleaf Panels."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        return self.async_create_entry(title="Nanoleaf Panels", data={})
