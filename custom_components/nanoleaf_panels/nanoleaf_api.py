"""Adapter for aionanoleaf2 device interactions, isolated in one place.

All calls to the private aionanoleaf2 ``_request`` method live here so that
any future breakage from library updates is contained to a single module.
The adapter is backed by the shared Home Assistant HTTP session, which avoids
creating a new ``ClientSession`` on every API call.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from aionanoleaf2 import Nanoleaf

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)


def _parse_anim_data(anim_data_str: str) -> dict[int, tuple[int, int, int, int, int]]:
    """Parse an animData string into {panel_id: (r, g, b, w, t)}."""
    parts = list(map(int, anim_data_str.split()))
    panel_colors: dict[int, tuple[int, int, int, int, int]] = {}
    idx = 0
    count = parts[idx]
    idx += 1
    for _ in range(count):
        panel_id = parts[idx]
        num_colors = parts[idx + 1]
        r, g, b, w, t = (
            parts[idx + 2],
            parts[idx + 3],
            parts[idx + 4],
            parts[idx + 5],
            parts[idx + 6],
        )
        panel_colors[panel_id] = (r, g, b, w, t)
        idx += 2 + num_colors * 5
    return panel_colors


class NanoleafApiClient:
    """Thin adapter over aionanoleaf2 using the shared HA HTTP session.

    All private ``_request`` calls are encapsulated here so any upstream API
    change only needs to be fixed in one place.  The underlying
    ``aiohttp.ClientSession`` is obtained via ``async_get_clientsession`` and
    is shared across the entire HA instance, eliminating connection-per-call
    overhead.
    """

    def __init__(self, hass: HomeAssistant, host: str, token: str) -> None:
        self._host = host
        self._nanoleaf = Nanoleaf(async_get_clientsession(hass), host, token)

    @property
    def host(self) -> str:
        """Return the device host address."""
        return self._host

    async def async_get_device_info(self) -> dict[str, Any]:
        """Fetch full device info from the Nanoleaf."""
        resp = await self._nanoleaf._request("get", "")
        data = cast(dict[str, Any], await resp.json())
        resp.release()
        return data

    async def async_get_panel_order(self) -> list[int]:
        """Return ordered list of panel IDs (excluding the controller panel 0)."""
        data = await self.async_get_device_info()
        try:
            return [
                cast(int, panel["panelId"])
                for panel in data["panelLayout"]["layout"]["positionData"]
                if cast(int, panel["panelId"]) != 0
            ]
        except KeyError as err:
            raise HomeAssistantError("Nanoleaf panel layout is not available") from err

    async def async_get_current_panel_colors(
        self,
    ) -> dict[int, tuple[int, int, int, int, int]]:
        """Request the current static panel colors. Returns {} if unavailable."""
        try:
            response = await self._nanoleaf._request(
                "put",
                "effects",
                {"write": {"command": "request", "animName": "*Static*"}},
            )
            data = cast(dict[str, Any], await response.json())
            response.release()
            anim_data_str = data.get("animData", "")
            if anim_data_str:
                return _parse_anim_data(anim_data_str)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Could not fetch current panel colors from %s: %s", self._host, err)
        return {}

    async def async_write_effects(self, payload: dict[str, Any]) -> None:
        """Send an effects write command to the Nanoleaf."""
        response = await self._nanoleaf._request("put", "effects", payload)
        response.release()
