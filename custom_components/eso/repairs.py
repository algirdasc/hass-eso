"""Repair flows for the ESO Energy Consumption integration."""

from __future__ import annotations

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict | None,
) -> RepairsFlow:
    """Create a fix flow for an ESO repair issue."""
    # All current issues (deprecated_yaml) are simple confirmations.
    return ConfirmRepairFlow()
