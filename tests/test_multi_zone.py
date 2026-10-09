"""Test the bhyve.start_multi_zone service."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bhyve.const import CONF_DEVICES, DOMAIN
from custom_components.bhyve.pybhyve.client import BHyveClient
from custom_components.bhyve.pybhyve.errors import BHyveError
from custom_components.bhyve.valve import SERVICE_START_MULTI_ZONE

if TYPE_CHECKING:
    from asyncio import AbstractEventLoop
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant


def make_sprinkler(device_id: str, mac: str, stations: list[str]) -> dict[str, Any]:
    """Build a sprinkler timer with one zone per station."""
    return {
        "id": device_id,
        "name": f"Sprinkler {device_id}",
        "type": "sprinkler_timer",
        "mac_address": mac,
        "is_connected": True,
        "zones": [
            {"station": station, "name": f"{device_id} zone {station}"}
            for station in stations
        ],
        "status": {"run_mode": "auto", "watering_status": None},
    }


SPRINKLER_A = make_sprinkler("device-a", "aabbccddee01", ["1", "2", "3"])
SPRINKLER_B = make_sprinkler("device-b", "aabbccddee02", ["1", "2"])


class FakeClient:
    """Stand-in for BHyveClient that serves two sprinklers and records sends."""

    instance: FakeClient | None = None

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        """Record the instance so tests can inspect calls."""
        self.send_multi_station_watering = AsyncMock()
        self.send_message = AsyncMock()
        FakeClient.instance = self

    async def login(self) -> bool:
        """Report a successful login."""
        return True

    def listen(self, loop: AbstractEventLoop, async_callback: Callable) -> None:
        """Do not open a websocket."""

    async def stop(self, *args: Any, **kwargs: Any) -> None:
        """Nothing to stop."""

    @property
    async def devices(self) -> list[dict[str, Any]]:
        """Return two sprinkler timers."""
        return [dict(SPRINKLER_A), dict(SPRINKLER_B)]

    @property
    async def timer_programs(self) -> list[dict[str, Any]]:
        """Return no programs."""
        return []

    async def get_device_history(self, *_args: Any, **_kwargs: Any) -> list:
        """Return no watering history."""
        return []

    async def get_landscape(self, *_args: Any, **_kwargs: Any) -> dict:
        """Return no landscape data."""
        return {}


@pytest.fixture
async def client(
    hass: HomeAssistant,
    enable_custom_integrations: None,  # noqa: ARG001
) -> FakeClient:
    """Set up the integration with both sprinklers and return the fake client."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_USERNAME: "user", CONF_PASSWORD: "password"},
        options={CONF_DEVICES: ["device-a", "device-b"]},
    )
    entry.add_to_hass(hass)

    with patch("custom_components.bhyve.BHyveClient", FakeClient):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert FakeClient.instance is not None
    return FakeClient.instance


def valve_id(hass: HomeAssistant, device: dict[str, Any], station: str) -> str:
    """Look up a zone valve's entity_id by its unique_id."""
    unique_id = f"{device['mac_address']}:{device['id']}:{station}:valve"
    entity_id = er.async_get(hass).async_get_entity_id("valve", DOMAIN, unique_id)
    assert entity_id is not None, f"no valve for {unique_id}"
    return entity_id


async def call_service(hass: HomeAssistant, stations: Any) -> None:
    """Call bhyve.start_multi_zone and wait for it to finish."""
    await hass.services.async_call(
        DOMAIN, SERVICE_START_MULTI_ZONE, {"stations": stations}, blocking=True
    )


@pytest.mark.usefixtures("client")
async def test_service_is_registered(hass: HomeAssistant) -> None:
    """Setting up the integration registers the service."""
    assert hass.services.has_service(DOMAIN, SERVICE_START_MULTI_ZONE)


async def test_single_device_sends_one_message_in_order(
    hass: HomeAssistant, client: FakeClient
) -> None:
    """Zones on one device go out in a single message, in the order given."""
    await call_service(
        hass,
        [
            {"entity_id": valve_id(hass, SPRINKLER_A, "3"), "minutes": 5},
            {"entity_id": valve_id(hass, SPRINKLER_A, "1"), "minutes": 10},
            {"entity_id": valve_id(hass, SPRINKLER_A, "2"), "minutes": 7},
        ],
    )

    client.send_multi_station_watering.assert_awaited_once_with(
        "device-a",
        [
            {"station": "3", "run_time": 5},
            {"station": "1", "run_time": 10},
            {"station": "2", "run_time": 7},
        ],
    )


async def test_multiple_devices_get_one_message_each(
    hass: HomeAssistant, client: FakeClient
) -> None:
    """Zones are grouped per device; each device gets its own message."""
    await call_service(
        hass,
        [
            {"entity_id": valve_id(hass, SPRINKLER_A, "1"), "minutes": 5},
            {"entity_id": valve_id(hass, SPRINKLER_B, "2"), "minutes": 8},
            {"entity_id": valve_id(hass, SPRINKLER_A, "2"), "minutes": 6},
            {"entity_id": valve_id(hass, SPRINKLER_B, "1"), "minutes": 4},
        ],
    )

    sent = {
        call.args[0]: call.args[1]
        for call in client.send_multi_station_watering.await_args_list
    }
    assert sent == {
        "device-a": [
            {"station": "1", "run_time": 5},
            {"station": "2", "run_time": 6},
        ],
        "device-b": [
            {"station": "2", "run_time": 8},
            {"station": "1", "run_time": 4},
        ],
    }


async def test_minutes_are_coerced_to_int(
    hass: HomeAssistant, client: FakeClient
) -> None:
    """Minutes given as a string (e.g. from a template) are accepted."""
    await call_service(
        hass, [{"entity_id": valve_id(hass, SPRINKLER_A, "1"), "minutes": "12"}]
    )

    client.send_multi_station_watering.assert_awaited_once_with(
        "device-a", [{"station": "1", "run_time": 12}]
    )


async def test_non_bhyve_entities_are_skipped(
    hass: HomeAssistant, client: FakeClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Unknown entities are logged and skipped; valid zones still run."""
    await call_service(
        hass,
        [
            {"entity_id": "valve.not_a_bhyve_zone", "minutes": 5},
            {"entity_id": valve_id(hass, SPRINKLER_A, "2"), "minutes": 9},
        ],
    )

    client.send_multi_station_watering.assert_awaited_once_with(
        "device-a", [{"station": "2", "run_time": 9}]
    )
    assert "valve.not_a_bhyve_zone is not a BHyve zone valve" in caplog.text


async def test_nothing_sent_when_no_valid_zones(
    hass: HomeAssistant, client: FakeClient, caplog: pytest.LogCaptureFixture
) -> None:
    """A call with no resolvable zones sends nothing."""
    await call_service(hass, [{"entity_id": "valve.not_a_bhyve_zone", "minutes": 5}])

    client.send_multi_station_watering.assert_not_awaited()
    assert "nothing sent" in caplog.text


async def test_failure_on_one_device_does_not_block_others(
    hass: HomeAssistant, client: FakeClient, caplog: pytest.LogCaptureFixture
) -> None:
    """A BHyveError for one device is logged; the other device still runs."""

    async def fail_for_a(device_id: str, _stations: list[dict]) -> None:
        if device_id == "device-a":
            msg = "socket closed"
            raise BHyveError(msg)

    client.send_multi_station_watering.side_effect = fail_for_a

    await call_service(
        hass,
        [
            {"entity_id": valve_id(hass, SPRINKLER_A, "1"), "minutes": 5},
            {"entity_id": valve_id(hass, SPRINKLER_B, "1"), "minutes": 5},
        ],
    )

    sent_to = {c.args[0] for c in client.send_multi_station_watering.await_args_list}
    assert sent_to == {"device-a", "device-b"}
    assert "Failed to send start_multi_zone for device device-a" in caplog.text


@pytest.mark.parametrize(
    "stations",
    [
        pytest.param([], id="empty list"),
        pytest.param([{"entity_id": "valve.x"}], id="missing minutes"),
        pytest.param([{"minutes": 5}], id="missing entity_id"),
        pytest.param([{"entity_id": "valve.x", "minutes": 0}], id="zero minutes"),
        pytest.param([{"entity_id": "valve.x", "minutes": -3}], id="negative minutes"),
        pytest.param([{"entity_id": "not an entity", "minutes": 5}], id="bad id"),
    ],
)
async def test_invalid_input_is_rejected(
    hass: HomeAssistant, client: FakeClient, stations: Any
) -> None:
    """Malformed service data fails validation before anything is sent."""
    with pytest.raises(vol.Invalid):
        await call_service(hass, stations)

    client.send_multi_station_watering.assert_not_awaited()


@pytest.mark.freeze_time("2026-06-01T12:34:56")
async def test_client_sends_change_mode_payload() -> None:
    """The client builds the change_mode websocket message the hub expects."""
    bhyve = BHyveClient("user", "pass", MagicMock())
    bhyve.send_message = AsyncMock()
    stations = [{"station": "1", "run_time": 5}, {"station": "3", "run_time": 10}]

    await bhyve.send_multi_station_watering("device-a", stations)

    bhyve.send_message.assert_awaited_once_with(
        {
            "event": "change_mode",
            "mode": "manual",
            "device_id": "device-a",
            "timestamp": "2026-06-01T12:34:56Z",
            "stations": stations,
        }
    )
