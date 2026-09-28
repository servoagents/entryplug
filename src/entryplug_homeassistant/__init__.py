"""Optional Home Assistant resource adapter; never imported by the portable core."""

from .light import HomeAssistantLight, HomeAssistantLightError

__all__ = ["HomeAssistantLight", "HomeAssistantLightError"]
