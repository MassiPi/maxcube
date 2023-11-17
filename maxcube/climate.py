"""Support for MAX! Thermostats via MAX! Cube."""
from __future__ import annotations

import logging
import time
import socket
from typing import Any

from .maxcube.device import (
    MAX_DEVICE_MODE_AUTOMATIC,
    MAX_DEVICE_MODE_BOOST,
    MAX_DEVICE_MODE_MANUAL,
    MAX_DEVICE_MODE_VACATION,
)

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    PRESET_BOOST,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_NONE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from . import DATA_KEY

_LOGGER = logging.getLogger(__name__)

ATTR_VALVE_POSITION = "valve_position"
ATTR_TEMPERATURE_OFFSET = "temp_offset"
ATTR_WINDOW_OPEN_TEMP = "window_open_temp"
ATTR_WINDOW_OPEN_DURATION = "window_open_duration"
ATTR_BOOST_VALUE = "boost_value"
ATTR_BOOST_DURATION = "boost_duration"
ATTR_DECALC_DAY = "decalc_day"
ATTR_DECALC_TIME = "decalc_time"
ATTR_MAX_VALVE = "max_valve"
ATTR_VALVE_OFFSET = "valve_offset"
ATTR_COMFORT_TEMP = "comfort_temp"
ATTR_ECO_TEMP = "eco_temp"
ATTR_ROOM = "room"
ATTR_DEVICE_ID = "device_id"
ATTR_DEVICE_RF_ADDRESS = "device_rf_address"
PRESET_ON = "On"
PRESET_WINDOW_OPEN = "Window Open"


# There are two magic temperature values, which indicate:
# Off (valve fully closed)
OFF_TEMPERATURE = 4.5
# On (valve fully open)
ON_TEMPERATURE = 30.5

# Lowest Value without turning off
MIN_TEMPERATURE = 5.0
# Largest Value without fully opening
MAX_TEMPERATURE = 30.0


def setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Iterate through all MAX! Devices and add thermostats."""
    devices = []
    for handler in hass.data[DATA_KEY].values():
        for device in handler.cube.devices:
            if device.is_thermostat() or device.is_wallthermostat():
                devices.append(MaxDeviceClimate(handler, device))

    devices.append(MaxCubeClimate(handler, handler.cube))
    add_entities(devices)
    
class MaxDeviceClimate(ClimateEntity):
    """MAX! Device ClimateEntity."""

    _attr_hvac_modes = [HVACMode.OFF, HVACMode.AUTO, HVACMode.HEAT]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE
    )

    def __init__(self, handler, device):
        """Initialize MAX! Cube ClimateEntity."""
        self._attr_room = device.room_id
        self.room = handler.cube.room_by_id(device.room_id)
        self._attr_name = f"{self.room.name} {device.name}"
        self._cubehandle = handler
        self._device = device
        self._attr_should_poll = True
        self._attr_unique_id = self._device.serial
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS
        self._attr_preset_modes = [
            PRESET_NONE, #probabilmente è mode auto
            PRESET_COMFORT,
            PRESET_ECO,
            PRESET_WINDOW_OPEN,
            PRESET_ON,
            PRESET_BOOST
        ]
        #need to fix the window open temp for wall thermostat
        if self._device.is_wallthermostat():
            for dev in self._cubehandle.cube.devices_by_room(self.room):
                if dev.is_thermostat() and dev.temperature_window_open > 0:
                    self._device.temperature_window_open = dev.temperature_window_open
                    break
                    
    @property
    def min_temp(self):
        """Return the minimum temperature."""
        temp = self._device.min_temperature or MIN_TEMPERATURE
        # OFF_TEMPERATURE (always off) a is valid temperature to maxcube but not to Home Assistant.
        # We use HVACMode.OFF instead to represent a turned off thermostat.
        return max(temp, MIN_TEMPERATURE)

    @property
    def max_temp(self):
        """Return the maximum temperature."""
        return self._device.max_temperature or MAX_TEMPERATURE

    @property
    def current_temperature(self):
        """Return the current temperature."""
        return self._device.actual_temperature

    @property
    def hvac_mode(self) -> HVACMode:
        """Return current operation mode."""
        mode = self._device.mode
        if mode in (MAX_DEVICE_MODE_AUTOMATIC, MAX_DEVICE_MODE_BOOST):
            return HVACMode.AUTO
        if (
            mode == MAX_DEVICE_MODE_MANUAL
            and self._device.target_temperature == OFF_TEMPERATURE
        ):
            return HVACMode.OFF

        return HVACMode.HEAT

    def set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target hvac mode."""
        if hvac_mode == HVACMode.OFF:
            self._set_target(MAX_DEVICE_MODE_MANUAL, OFF_TEMPERATURE)
        elif hvac_mode == HVACMode.HEAT:
            temp = max(self._device.target_temperature, self.min_temp)
            self._set_target(MAX_DEVICE_MODE_MANUAL, temp)
        elif hvac_mode == HVACMode.AUTO:
            self._set_target(MAX_DEVICE_MODE_AUTOMATIC, None)
        else:
            raise ValueError(f"unsupported HVAC mode {hvac_mode}")

    def _set_target(self, mode: int | None, temp: float | None) -> None:
        """Set the mode and/or temperature of the thermostat.

        @param mode: this is the mode to change to.
        @param temp: the temperature to target.

        Both parameters are optional. When mode is undefined, it keeps
        the previous mode. When temp is undefined, it fetches the
        temperature from the weekly schedule when mode is
        MAX_DEVICE_MODE_AUTOMATIC and keeps the previous
        temperature otherwise.
        """
        with self._cubehandle.mutex:
            try:
                self._cubehandle.cube.set_temperature_mode(self._device, temp, mode)
            except (socket.timeout, OSError):
                _LOGGER.error("Setting HVAC mode failed")
        time.sleep(2)
        self.update()

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return the current running hvac operation if supported."""
        valve = 0

        if self._device.is_thermostat():
            valve = self._device.valve_position
        elif self._device.is_wallthermostat():
            cube = self._cubehandle.cube
            room = cube.room_by_id(self._device.room_id)
            for device in cube.devices_by_room(room):
                if device.is_thermostat() and device.valve_position > 0:
                    valve = device.valve_position
                    break
        #else:
        #    return None

        # Assume heating when valve is open
        if valve > 0:
            return HVACAction.HEATING

        return HVACAction.OFF if self.hvac_mode == HVACMode.OFF else HVACAction.IDLE

    @property
    def target_temperature(self):
        """Return the temperature we try to reach."""
        temp = self._device.target_temperature
        if temp is None or temp < self.min_temp or temp > self.max_temp:
            return None
        return temp

    def set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperatures."""
        if (temp := kwargs.get(ATTR_TEMPERATURE)) is None:
            raise ValueError(
                f"No {ATTR_TEMPERATURE} parameter passed to set_temperature method."
            )
        if ( kwargs.get(ATTR_HVAC_MODE) is not None ):
            self._set_target(kwargs.get(ATTR_HVAC_MODE), temp)
        else:
            self._set_target(None, temp)

    @property
    def preset_mode(self):
        """Return the current preset mode."""
        if self._device.mode == MAX_DEVICE_MODE_BOOST:
            return PRESET_BOOST
        elif self._device.target_temperature == self._device.comfort_temperature:
            return PRESET_COMFORT
        elif self._device.target_temperature == self._device.eco_temperature:
            return PRESET_ECO
        elif self._device.target_temperature == ON_TEMPERATURE:
            return PRESET_ON
        elif self._device.target_temperature == self._device.temperature_window_open:
            return PRESET_WINDOW_OPEN
        return PRESET_NONE #auto?

    def set_preset_mode(self, preset_mode: str) -> None:
        """Set new operation mode."""
        if preset_mode == PRESET_COMFORT:
            self._set_target(None, self._device.comfort_temperature)
        elif preset_mode == PRESET_ECO:
            self._set_target(None, self._device.eco_temperature)
        elif preset_mode == PRESET_ON:
            self._set_target(None, ON_TEMPERATURE)
        elif preset_mode == PRESET_NONE: #auto?
            self._set_target(MAX_DEVICE_MODE_AUTOMATIC, None)
        elif preset_mode == PRESET_BOOST: #only way to have boost
            self._set_target(MAX_DEVICE_MODE_BOOST, None)
        elif preset_mode == PRESET_WINDOW_OPEN:
            self._set_target(None, self._device.temperature_window_open)
        else:
            raise ValueError(f"unsupported preset mode {preset_mode}")

    @property
    def extra_state_attributes(self):
        """Return the optional state attributes."""
        if self._device.is_thermostat():
            return {ATTR_VALVE_POSITION: self._device.valve_position,
                    ATTR_WINDOW_OPEN_TEMP: self._device.temperature_window_open,
                    ATTR_TEMPERATURE_OFFSET: self._device.temperature_offset,
                    ATTR_WINDOW_OPEN_DURATION: self._device.window_open_duration,
                    ATTR_BOOST_VALUE: self._device.boost_value,
                    ATTR_BOOST_DURATION: self._device.boost_duration,
                    ATTR_DECALC_DAY: self._device.decalc_day,
                    ATTR_DECALC_TIME: self._device.decalc_time,
                    ATTR_MAX_VALVE: self._device.max_valve,
                    ATTR_VALVE_OFFSET: self._device.valve_offset,
                    ATTR_COMFORT_TEMP: self._device.comfort_temperature,
                    ATTR_ECO_TEMP: self._device.eco_temperature,
                    ATTR_ROOM: self._attr_room,
                    ATTR_DEVICE_ID: self._attr_unique_id,
                    ATTR_DEVICE_RF_ADDRESS: self._device.rf_address
                    }
        
        elif self._device.is_wallthermostat():
            cube = self._cubehandle.cube
            room = cube.room_by_id(self._device.room_id)
            #taking useful properties from thermostat to wall thermostat
            for device in cube.devices_by_room(room):
                if device.is_thermostat():
                    valve_pos = device.valve_position
                    break
            return {ATTR_VALVE_POSITION: valve_pos,
                    ATTR_WINDOW_OPEN_TEMP: self._device.temperature_window_open,
                    ATTR_COMFORT_TEMP: self._device.comfort_temperature,
                    ATTR_ECO_TEMP: self._device.eco_temperature,
                    ATTR_ROOM: self._attr_room,
                    ATTR_DEVICE_ID: self._attr_unique_id,
                    ATTR_DEVICE_RF_ADDRESS: self._device.rf_address
                    }        
        else:
            return {}
       
    def update(self) -> None:
        """Get latest data from MAX! Cube."""
        self._cubehandle.update()
        #need to fix the window open temp for wall thermostat
        if self._device.is_wallthermostat():
            for dev in self._cubehandle.cube.devices_by_room(self.room):
                if dev.is_thermostat() and dev.temperature_window_open > 0:
                    self._device.temperature_window_open = dev.temperature_window_open
                    break

class MaxCubeClimate(ClimateEntity):
    """MAX! Device ClimateEntity."""

    _attr_hvac_modes = [HVACMode.OFF, HVACMode.AUTO, HVACMode.HEAT]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE
    )

    def __init__(self, handler, device):
        """Initialize MAX! Cube ClimateEntity."""
        room = None
        self._attr_name = f"Home Cube"
        self._attr_should_poll = True
        self._cubehandle = handler
        self._device = device
        self._attr_unique_id = self._device.serial
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS
        self._attr_preset_modes = [
            PRESET_NONE, #probabilmente è mode auto
            PRESET_COMFORT,
            PRESET_ECO,
            PRESET_WINDOW_OPEN,
            PRESET_ON,
            PRESET_BOOST
        ]
        #need to fix the temps for Cube. Taking data from devices
        self._device.eco_temperature = 0.0
        self._device.comfort_temperature = 0.0
        self._device.temperature_window_open = 0.0
        self._device.min_temperature = MIN_TEMPERATURE
        #self._device.max_temperature = MAX_TEMPERATURE
        self._device.max_temperature = ON_TEMPERATURE
        #fake
        self._device.mode = MAX_DEVICE_MODE_AUTOMATIC
        self._device.target_temperature = (MIN_TEMPERATURE+MAX_TEMPERATURE)/2

        for device in self._device.devices:
            if device.is_thermostat() or device.is_wallthermostat():
                # i assume every value in the system is good enough to be used for the whole home
                self._device.eco_temperature = max(self._device.eco_temperature, device.eco_temperature)
                self._device.comfort_temperature = max(self._device.comfort_temperature, device.comfort_temperature)
                self._device.temperature_window_open = max(self._device.temperature_window_open, device.temperature_window_open)

    @property
    def min_temp(self):
        """Return the minimum temperature."""
        temp = self._device.min_temperature or MIN_TEMPERATURE
        # OFF_TEMPERATURE (always off) a is valid temperature to maxcube but not to Home Assistant.
        # We use HVACMode.OFF instead to represent a turned off thermostat.
        return max(temp, MIN_TEMPERATURE)

    @property
    def max_temp(self):
        """Return the maximum temperature."""
        return self._device.max_temperature or MAX_TEMPERATURE

    @property
    def hvac_mode(self) -> HVACMode:
        """Return current operation mode."""
        mode = self._device.mode
        if mode in (MAX_DEVICE_MODE_AUTOMATIC, MAX_DEVICE_MODE_BOOST):
            return HVACMode.AUTO
        if (
            mode == MAX_DEVICE_MODE_MANUAL
            and self._device.target_temperature == OFF_TEMPERATURE
        ):
            return HVACMode.OFF

        return HVACMode.HEAT

    def set_hvac_mode(self, hvac_mode: HVACMode) -> None: # THIS -< add device target_temperature
        """Set new target hvac mode."""
        if hvac_mode == HVACMode.OFF:
            self._device.target_temperature = OFF_TEMPERATURE
            self._device.mode = MAX_DEVICE_MODE_MANUAL
            self._set_target(MAX_DEVICE_MODE_MANUAL, OFF_TEMPERATURE)
        elif hvac_mode == HVACMode.HEAT:
            self._device.mode = MAX_DEVICE_MODE_MANUAL
            self._set_target(MAX_DEVICE_MODE_MANUAL, self._device.target_temperature)
        elif hvac_mode == HVACMode.AUTO:
            self._device.mode = MAX_DEVICE_MODE_AUTOMATIC
            self._device.target_temperature = (MIN_TEMPERATURE+MAX_TEMPERATURE)/2
            self._set_target(MAX_DEVICE_MODE_AUTOMATIC, 0)
        else:
            raise ValueError(f"unsupported HVAC mode {hvac_mode}")

    def _set_target(self, mode: int, temp: float ) -> None: #THIS
        with self._cubehandle.mutex:
            try:
                self._cubehandle.cube.set_temperature_mode(self._device, temp, mode)
            except (socket.timeout, OSError):
                _LOGGER.error("Setting HVAC mode failed")
        time.sleep(2)
        self.update()

    @property
    def target_temperature(self):
        """Return the temperature we try to reach."""
        temp = self._device.target_temperature
        if temp is None or temp < self.min_temp or temp > self.max_temp:
            return None
        return temp

    def set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperatures."""
        if (temp := kwargs.get(ATTR_TEMPERATURE)) is None:
            raise ValueError(
                f"No {ATTR_TEMPERATURE} parameter passed to set_temperature method."
            )
        self._device.target_temperature = temp
        if ( kwargs.get(ATTR_HVAC_MODE) is not None ):
            self._set_target(kwargs.get(ATTR_HVAC_MODE), temp)
        else:
            self._set_target(self._device.mode, temp)

    @property
    def preset_mode(self):
        """Return the current preset mode."""
        if self._device.target_temperature == self._device.comfort_temperature:
            return PRESET_COMFORT
        elif self._device.target_temperature == self._device.eco_temperature:
            return PRESET_ECO
        elif self._device.target_temperature == ON_TEMPERATURE:
            return PRESET_ON
        elif self._device.target_temperature == self._device.temperature_window_open:
            return PRESET_WINDOW_OPEN
        return PRESET_NONE #auto?
        
    def set_preset_mode(self, preset_mode: str) -> None: #THIS
        """Set new operation mode."""
        if preset_mode == PRESET_COMFORT:
            self._device.target_temperature = self._device.comfort_temperature
            self._set_target(self._device.mode, self._device.comfort_temperature) #perchè cambia mode?
        elif preset_mode == PRESET_ECO:
            self._device.target_temperature = self._device.eco_temperature
            self._set_target(self._device.mode, self._device.eco_temperature) #perchè cambia mode?
        elif preset_mode == PRESET_ON:
            self._device.target_temperature = ON_TEMPERATURE
            self._set_target(self._device.mode, ON_TEMPERATURE)
        elif preset_mode == PRESET_NONE: #auto?
            self._device.mode = MAX_DEVICE_MODE_AUTOMATIC
            self._device.target_temperature = (MIN_TEMPERATURE+MAX_TEMPERATURE)/2
            self._set_target(MAX_DEVICE_MODE_AUTOMATIC, 0)
        elif preset_mode == PRESET_BOOST: #only way to have boost
            self._device.mode = MAX_DEVICE_MODE_BOOST
            self._set_target(MAX_DEVICE_MODE_BOOST, self._device.target_temperature)
        elif preset_mode == PRESET_WINDOW_OPEN:
            self._device.target_temperature = self._device.temperature_window_open
            self._set_target(self._device.mode, self._device.temperature_window_open)
        else:
            raise ValueError(f"unsupported preset mode {preset_mode}")

    @property
    def extra_state_attributes(self):
        """Return the optional state attributes."""
        return {ATTR_WINDOW_OPEN_TEMP: self._device.temperature_window_open,
                ATTR_COMFORT_TEMP: self._device.comfort_temperature,
                ATTR_ECO_TEMP: self._device.eco_temperature,
                ATTR_DEVICE_ID: self._attr_unique_id,
                ATTR_DEVICE_RF_ADDRESS: self._device.rf_address
                }
       
    def update(self) -> None:
        """Get latest data from MAX! Cube."""
        self._device.update()
        for device in self._device.devices:
            if device.is_thermostat() or device.is_wallthermostat():
                # i assume every value in the system is good enough to be used for the whole home
                self._device.eco_temperature = max(self._device.eco_temperature, device.eco_temperature)
                self._device.comfort_temperature = max(self._device.comfort_temperature, device.comfort_temperature)
                self._device.temperature_window_open = max(self._device.temperature_window_open, device.temperature_window_open)