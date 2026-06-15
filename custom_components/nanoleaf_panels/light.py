"""Per-panel light entities for Nanoleaf Panels integration."""

from __future__ import annotations

import colorsys
from datetime import timedelta
import logging
import math
from typing import Any

from aiohttp import ClientSession
from aionanoleaf2 import Nanoleaf

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_HS_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_TOKEN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from . import (
    CONF_EXPOSE_PANELS,
    CONF_NANOLEAF_ENTITY,
    DEFAULT_TRANSITION_TIME,
    DOMAIN,
    _async_get_current_panel_colors,
    _async_get_panel_order,
    _async_write_panels,
)

_LOGGER = logging.getLogger(__name__)


def _kelvin_to_rgb(kelvin: int, brightness: float) -> tuple[int, int, int]:
    """Approximate Kelvin color temperature → (r, g, b); brightness in 0‒1."""
    temp = kelvin / 100
    if temp <= 66:
        r = 255
        g = max(0, min(255, round(99.4708025861 * math.log(temp) - 161.1195681661)))
        b = 0 if temp <= 19 else max(0, min(255, round(138.5177312231 * math.log(temp - 10) - 305.0447927307)))
    else:
        r = max(0, min(255, round(329.698727446 * (temp - 60) ** -0.1332047592)))
        g = max(0, min(255, round(288.1221695283 * (temp - 60) ** -0.0755148492)))
        b = 255
    return (round(r * brightness), round(g * brightness), round(b * brightness))


class NanoleafPanelCoordinator(
    DataUpdateCoordinator[dict[int, tuple[int, int, int, int, int]]]
):
    """Polls the Nanoleaf device for per-panel color state."""

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        token: str,
        nanoleaf_entry_id: str,
        nanoleaf_entity_id: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"Nanoleaf panels ({host})",
            update_interval=timedelta(seconds=30),
        )
        self._host = host
        self._token = token
        self._nanoleaf_entry_id = nanoleaf_entry_id
        self.nanoleaf_entity_id = nanoleaf_entity_id
        self.panel_ids: list[int] = []
        # True only when the most recent poll returned actual per-panel colors
        # from the device (i.e. device was in static/custom mode).  False means
        # the device is in effect mode or just turned on — cache cannot be
        # trusted to reflect which individual panels are lit.
        self.last_poll_had_device_data: bool = False

    async def _async_update_data(self) -> dict[int, tuple[int, int, int, int, int]]:
        """Poll current panel colors; merge with in-memory cache as fallback."""
        cache: dict[int, tuple[int, int, int, int, int]] = (
            self.hass.data.get(DOMAIN, {})
            .get("panel_state", {})
            .get(self._nanoleaf_entry_id, {})
        )

        try:
            async with ClientSession() as session:
                nanoleaf = Nanoleaf(session, self._host, self._token)

                # Fetch full device info to determine color mode.
                resp = await nanoleaf._request("get", "")
                info: dict = await resp.json()
                resp.release()

                state = info.get("state", {})
                selected_effect = info.get("effects", {}).get("selectedEffect")
                color_mode = state.get("colorMode", "")

                if selected_effect == "*Static*":
                    # Device is running our custom per-panel animation.
                    device_colors = await _async_get_current_panel_colors(nanoleaf)
                    self.last_poll_had_device_data = bool(device_colors)
                    return {**cache, **device_colors}

                if color_mode in ("hs", "ct"):
                    # Uniform color set via the official integration.
                    # Derive per-panel color from device state so all panels
                    # reflect the correct color and brightness.
                    bri = state.get("brightness", {}).get("value", 100) / 100
                    if color_mode == "hs":
                        h = state.get("hue", {}).get("value", 0) / 360
                        s = state.get("sat", {}).get("value", 100) / 100
                        r, g, b = colorsys.hsv_to_rgb(h, s, bri)
                        rgb = (round(r * 255), round(g * 255), round(b * 255))
                    else:
                        ct = state.get("ct", {}).get("value", 4000)
                        rgb = _kelvin_to_rgb(ct, bri)
                    uniform: dict[int, tuple[int, int, int, int, int]] = {
                        pid: (*rgb, 0, DEFAULT_TRANSITION_TIME)
                        for pid in self.panel_ids
                    }
                    self.last_poll_had_device_data = True
                    return {**cache, **uniform}

        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(f"Failed to fetch Nanoleaf state: {err}") from err

        # Effect mode with a non-static animation — per-panel colors unavailable.
        self.last_poll_had_device_data = False
        return cache


class NanoleafPanelLight(
    CoordinatorEntity[NanoleafPanelCoordinator], LightEntity
):
    """A light entity representing a single Nanoleaf panel."""

    _attr_color_mode = ColorMode.HS
    _attr_supported_color_modes = {ColorMode.HS}
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: NanoleafPanelCoordinator,
        entry: ConfigEntry,
        panel_id: int,
        panel_number: int,
        device_info: DeviceInfo | None,
    ) -> None:
        super().__init__(coordinator)
        self._panel_id = panel_id
        self._attr_name = f"Panel {panel_number}"
        self._attr_unique_id = f"{entry.entry_id}_panel_{panel_id}"
        if device_info is not None:
            self._attr_device_info = device_info

    @property
    def _panel_state(self) -> tuple[int, int, int, int, int] | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self._panel_id)

    @property
    def is_on(self) -> bool:
        # Mirror the parent Nanoleaf entity's on/off state so turning off the
        # device via the official integration is immediately reflected here.
        parent = self.hass.states.get(self.coordinator.nanoleaf_entity_id)
        if parent is None or parent.state in ("off", "unavailable", "unknown"):
            return False
        # If the last poll returned no device data the Nanoleaf is in effect
        # mode (or just turned on) and the cache cannot be trusted to reflect
        # which individual panels are lit.  Show all panels as on so they
        # don't wrongly stay "off" because of a stale all-zero cache.
        if not self.coordinator.last_poll_had_device_data:
            # Device data unavailable (effect mode or mid-transition).
            # If the cache explicitly shows this panel as black (e.g. we just
            # turned it off), respect that; otherwise assume on.
            s = self._panel_state
            if s is not None and s[0] == 0 and s[1] == 0 and s[2] == 0:
                return False
            return True
        s = self._panel_state
        return s is not None and (s[0] > 0 or s[1] > 0 or s[2] > 0)

    @property
    def hs_color(self) -> tuple[float, float] | None:
        s = self._panel_state
        if s is None or (s[0] == 0 and s[1] == 0 and s[2] == 0):
            return None
        h, sat, _ = colorsys.rgb_to_hsv(s[0] / 255, s[1] / 255, s[2] / 255)
        return (h * 360, sat * 100)

    @property
    def brightness(self) -> int | None:
        s = self._panel_state
        if s is None:
            return None
        _, _, v = colorsys.rgb_to_hsv(s[0] / 255, s[1] / 255, s[2] / 255)
        return round(v * 255)

    async def async_turn_on(self, **kwargs: Any) -> None:
        hs: tuple[float, float] | None = kwargs.get(ATTR_HS_COLOR)
        brightness: int | None = kwargs.get(ATTR_BRIGHTNESS)

        # Decompose current state into HSV so each axis is updated independently.
        # This avoids cascading scaling errors: e.g. dimming to 50% then back to
        # 75% correctly restores to 75% of the original color, not 75% of the
        # already-dimmed value.
        s = self._panel_state
        if s and (s[0] > 0 or s[1] > 0 or s[2] > 0):
            cur_h, cur_s, cur_v = colorsys.rgb_to_hsv(
                s[0] / 255, s[1] / 255, s[2] / 255
            )
        else:
            cur_h, cur_s, cur_v = 0.0, 0.0, 1.0  # default: white

        new_h = (hs[0] / 360) if hs is not None else cur_h
        new_s = (hs[1] / 100) if hs is not None else cur_s
        new_v = (brightness / 255) if brightness is not None else cur_v

        r_f, g_f, b_f = colorsys.hsv_to_rgb(new_h, new_s, new_v)
        r, g, b = round(r_f * 255), round(g_f * 255), round(b_f * 255)

        # When the parent Nanoleaf is off, all other panels must be forced to
        # black so that only this panel lights up (not the full cached scene).
        parent = self.hass.states.get(self.coordinator.nanoleaf_entity_id)
        if parent is None or parent.state != "on":
            panels_override: dict[int, list[tuple[int, int, int, int, int]]] = {
                pid: [(0, 0, 0, 0, DEFAULT_TRANSITION_TIME)]
                for pid in self.coordinator.panel_ids
                if pid != self._panel_id
            }
            panels_override[self._panel_id] = [(r, g, b, 0, DEFAULT_TRANSITION_TIME)]
        else:
            panels_override = {self._panel_id: [(r, g, b, 0, DEFAULT_TRANSITION_TIME)]}

        # Seed the cache with the coordinator's current per-panel state so
        # _async_write_panels uses the right colors for non-overridden panels
        # instead of potentially stale values from previous writes.
        if self.coordinator.data:
            self.hass.data.setdefault(DOMAIN, {}).setdefault("panel_state", {})[
                self.coordinator._nanoleaf_entry_id
            ] = dict(self.coordinator.data)

        async with ClientSession() as session:
            nl = Nanoleaf(session, self.coordinator._host, self.coordinator._token)
            await _async_write_panels(
                self.hass,
                nl,
                self.coordinator._nanoleaf_entry_id,
                self.coordinator.panel_ids,
                panels_override,
            )
        # Push the written state directly into the coordinator instead of
        # re-polling the device.  This avoids a race where the device hasn't
        # updated its *Static* animation yet when we immediately read it back.
        new_data = dict(
            self.hass.data.get(DOMAIN, {})
            .get("panel_state", {})
            .get(self.coordinator._nanoleaf_entry_id, {})
        )
        self.coordinator.last_poll_had_device_data = True
        self.coordinator.async_set_updated_data(new_data)

    async def async_turn_off(self, **kwargs: Any) -> None:
        # If the parent Nanoleaf is already off, all panel entities already
        # report off — nothing to write to the device.
        parent = self.hass.states.get(self.coordinator.nanoleaf_entity_id)
        if parent is None or parent.state != "on":
            return

        # Seed the cache with the coordinator's current per-panel state so
        # _async_write_panels uses the right colors for non-overridden panels.
        if self.coordinator.data:
            self.hass.data.setdefault(DOMAIN, {}).setdefault("panel_state", {})[
                self.coordinator._nanoleaf_entry_id
            ] = dict(self.coordinator.data)

        async with ClientSession() as session:
            nl = Nanoleaf(session, self.coordinator._host, self.coordinator._token)
            await _async_write_panels(
                self.hass,
                nl,
                self.coordinator._nanoleaf_entry_id,
                self.coordinator.panel_ids,
                {self._panel_id: [(0, 0, 0, 0, DEFAULT_TRANSITION_TIME)]},
            )

        # Push the written state directly into the coordinator.
        new_data = dict(
            self.hass.data.get(DOMAIN, {})
            .get("panel_state", {})
            .get(self.coordinator._nanoleaf_entry_id, {})
        )
        self.coordinator.last_poll_had_device_data = True
        self.coordinator.async_set_updated_data(new_data)

        # If every panel is now black, turn off the parent Nanoleaf entity.
        all_off = all(
            new_data.get(pid, (0, 0, 0, 0, 0))[:3] == (0, 0, 0)
            for pid in self.coordinator.panel_ids
        )
        if all_off:
            await self.hass.services.async_call(
                "light",
                "turn_off",
                {"entity_id": self.coordinator.nanoleaf_entity_id},
            )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up panel light entities from a config entry."""
    if not entry.options.get(CONF_EXPOSE_PANELS):
        return

    nanoleaf_entity_id: str | None = entry.options.get(CONF_NANOLEAF_ENTITY)
    if not nanoleaf_entity_id:
        _LOGGER.warning(
            "Panel entities are enabled but no Nanoleaf entity is configured"
        )
        return

    entity_entry = er.async_get(hass).async_get(nanoleaf_entity_id)
    if entity_entry is None:
        _LOGGER.error(
            "Configured Nanoleaf entity '%s' not found in registry", nanoleaf_entity_id
        )
        return

    nanoleaf_config_entry = hass.config_entries.async_get_entry(
        entity_entry.config_entry_id
    )
    if nanoleaf_config_entry is None or nanoleaf_config_entry.domain != "nanoleaf":
        _LOGGER.error(
            "Could not find a Nanoleaf config entry for entity '%s'",
            nanoleaf_entity_id,
        )
        return

    host: str = nanoleaf_config_entry.data[CONF_HOST]
    token: str = nanoleaf_config_entry.data[CONF_TOKEN]
    nanoleaf_entry_id: str = nanoleaf_config_entry.entry_id

    # Link panel entities to the existing Nanoleaf device so entity IDs are
    # scoped per device (e.g. light.nanoleaf_pascal_panel_1).
    device_info: DeviceInfo | None = None
    if entity_entry.device_id:
        device_entry = dr.async_get(hass).async_get(entity_entry.device_id)
        if device_entry is not None:
            device_info = DeviceInfo(identifiers=device_entry.identifiers)

    try:
        async with ClientSession() as session:
            nl = Nanoleaf(session, host, token)
            panel_ids = await _async_get_panel_order(nl)
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("Failed to fetch Nanoleaf panel layout: %s", err)
        return

    coordinator = NanoleafPanelCoordinator(
        hass, host, token, nanoleaf_entry_id, nanoleaf_entity_id
    )
    coordinator.panel_ids = panel_ids
    await coordinator.async_config_entry_first_refresh()

    # When the parent Nanoleaf entity changes state (on ↔ off ↔ unavailable):
    # - Turning OFF: re-broadcast current data immediately so panel entities
    #   reflect "off" without waiting for the next 30 s poll.
    # - Turning ON:  trigger a real device poll so panel entities pick up the
    #   actual current colors (works well in static mode; in effect mode the
    #   poll returns nothing and panels keep their last cached state).
    @callback
    def _on_nanoleaf_state_change(event: Any) -> None:
        new_state = event.data.get("new_state")
        if new_state is not None and new_state.state == "on":
            coordinator.hass.async_create_task(coordinator.async_request_refresh())
        else:
            coordinator.async_set_updated_data(coordinator.data or {})

    entry.async_on_unload(
        async_track_state_change_event(
            hass, [nanoleaf_entity_id], _on_nanoleaf_state_change
        )
    )

    async_add_entities(
        [
            NanoleafPanelLight(coordinator, entry, panel_id, idx + 1, device_info)
            for idx, panel_id in enumerate(panel_ids)
        ]
    )
