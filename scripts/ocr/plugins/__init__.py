"""Airline parser plugins."""

from __future__ import annotations

from .base import AirlinePlugin
from .buddha import BuddhaPlugin
from .yeti import YetiPlugin

_PLUGINS: dict[str, AirlinePlugin] = {
    "buddha": BuddhaPlugin(),
    "yeti": YetiPlugin(),
}


def get_plugin(airline: str) -> AirlinePlugin:
    key = airline.strip().lower()
    for name, plugin in _PLUGINS.items():
        if key.startswith(name):
            return plugin
    supported = ", ".join(sorted(_PLUGINS))
    raise ValueError(f"Unsupported airline template: {airline}. Supported: {supported}")


def list_plugins() -> list[str]:
    return sorted(_PLUGINS)
