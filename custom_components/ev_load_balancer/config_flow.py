"""Config Flow för EV Load Balancer.

Hanterar 6-stegs konfigurationsguide via HA:s UI samt Options Flow
för efterkonfiguration av fassensorer och beräkningsparametrar.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

from .charger_profiles import PROFILES
from .const import (
    CONF_CAPACITY_WARNING_THRESHOLD,
    CONF_CHARGER_ENTITIES,
    CONF_MAX_CURRENT,
    CONF_MIN_CURRENT,
    CONF_PHASE_COUNT,
    CONF_PHASES,
    CONF_PROFILE_ID,
    CONF_SAFETY_MARGIN,
    CONF_SERIAL,
    DEFAULT_CAPACITY_WARNING_THRESHOLD,
    DEFAULT_MAX_CURRENT,
    DEFAULT_MIN_CURRENT,
    DEFAULT_PHASE_COUNT,
    DEFAULT_SAFETY_MARGIN,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class EVLoadBalancerConfigFlow(ConfigFlow, domain=DOMAIN):
    """6-stegs konfigurationsguide för EV Load Balancer.

    Steg 1: Välj laddarprofil
    Steg 2: Serienummer (villkorligt — visas bara om profil kräver det)
    Steg 3: Verifiera/justera laddarentiteter
    Steg 4: Konfigurera fassensorer (SÄKERHETSKRITISKT)
    Steg 5: Beräkningsparametrar
    Steg 6: Bekräftelse och skapande av config entry
    """

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Initialisera config flow med tom dataackumulator."""
        self._data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Steg 1: Välj laddarprofil."""
        if user_input is not None:
            profile_id = user_input[CONF_PROFILE_ID]
            self._data[CONF_PROFILE_ID] = profile_id
            profile = PROFILES[profile_id]

            if profile.requires_serial:
                return await self.async_step_serial()
            # Generic-profil — hoppa steg 2
            return await self.async_step_entities()

        # Bygg profilvalsalternativ från PROFILES-konstanten
        profile_options = [{"value": pid, "label": prof.name} for pid, prof in PROFILES.items()]

        schema = vol.Schema(
            {
                vol.Required(CONF_PROFILE_ID): SelectSelector(
                    SelectSelectorConfig(
                        options=profile_options,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )

        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_serial(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Steg 2: Serienummer (visas bara om profil kräver det)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            serial = user_input.get(CONF_SERIAL, "").strip()
            if not serial:
                errors[CONF_SERIAL] = "empty_serial"
            else:
                self._data[CONF_SERIAL] = serial
                profile = PROFILES[self._data[CONF_PROFILE_ID]]
                # Lös entitets-ID:n via profilen och spara
                self._data[CONF_CHARGER_ENTITIES] = profile.resolve(serial)
                return await self.async_step_entities()

        schema = vol.Schema(
            {
                vol.Required(CONF_SERIAL): TextSelector(TextSelectorConfig()),
            }
        )

        return self.async_show_form(
            step_id="serial",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Steg 3: Verifiera/justera laddarentiteter."""
        if user_input is not None:
            self._data[CONF_CHARGER_ENTITIES] = user_input
            return await self.async_step_phases()

        # Pre-fill från tidigare resolve() eller tom dict för generic-profil
        pre_fill = self._data.get(CONF_CHARGER_ENTITIES, {})

        schema = vol.Schema(
            {
                vol.Optional("amp", default=pre_fill.get("amp", "")): EntitySelector(
                    EntitySelectorConfig(domain="number")
                ),
                vol.Optional("frc", default=pre_fill.get("frc", "")): EntitySelector(
                    EntitySelectorConfig(domain="select")
                ),
                vol.Optional("psm", default=pre_fill.get("psm", "")): EntitySelector(
                    EntitySelectorConfig(domain="select")
                ),
                vol.Optional("car_value", default=pre_fill.get("car_value", "")): EntitySelector(
                    EntitySelectorConfig()
                ),
                vol.Optional("nrg_4", default=pre_fill.get("nrg_4", "")): EntitySelector(
                    EntitySelectorConfig()
                ),
                vol.Optional("nrg_5", default=pre_fill.get("nrg_5", "")): EntitySelector(
                    EntitySelectorConfig()
                ),
                vol.Optional("nrg_6", default=pre_fill.get("nrg_6", "")): EntitySelector(
                    EntitySelectorConfig()
                ),
                vol.Optional("map", default=pre_fill.get("map", "")): EntitySelector(
                    EntitySelectorConfig()
                ),
            }
        )

        return self.async_show_form(step_id="entities", data_schema=schema)

    async def async_step_phases(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Steg 4: Konfigurera fassensorer (SÄKERHETSKRITISKT).

        Minst 1 fas måste konfigureras. Duplikat-sensorer blockeras hårt.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            # Samla alla angivna sensor-entiteter
            sensors = [
                v
                for k, v in user_input.items()
                if k.endswith("_sensor") and v is not None and v != ""
            ]

            if len(sensors) == 0:
                errors["base"] = "no_phases"
            elif len(sensors) != len(set(sensors)):
                # Säkerhetskritisk: duplikat-sensor blockeras
                errors["base"] = "duplicate_phase_sensors"
            else:
                # Konvertera platt formulärstruktur till phases-lista
                phases = []
                for i in range(1, 4):
                    sensor = user_input.get(f"phase_{i}_sensor")
                    max_a = user_input.get(f"phase_{i}_max_ampere")
                    if sensor is not None and sensor != "":
                        phases.append(
                            {
                                "sensor": sensor,
                                "max_ampere": int(max_a) if max_a is not None else 25,
                                "label": f"L{i}",
                            }
                        )
                self._data[CONF_PHASES] = phases
                return await self.async_step_params()

        # Befintliga värden för re-visning vid fel (eller första visning)
        existing = self._data.get(CONF_PHASES, [])
        phase_defaults: dict[str, Any] = {}
        for idx, phase in enumerate(existing[:3], start=1):
            phase_defaults[f"phase_{idx}_sensor"] = phase.get("sensor", "")
            phase_defaults[f"phase_{idx}_max_ampere"] = phase.get("max_ampere", 25)

        # Bygg summering för description_placeholders
        configured_count = len(
            [k for k, v in phase_defaults.items() if k.endswith("_sensor") and v]
        )
        summary = (
            f"{configured_count} fas(er) konfigurerade"
            if configured_count > 0
            else "Ingen fas konfigurerad"
        )

        schema = vol.Schema(
            {
                vol.Required(
                    "phase_1_sensor",
                    default=phase_defaults.get("phase_1_sensor", vol.UNDEFINED),
                ): EntitySelector(EntitySelectorConfig(domain="sensor")),
                vol.Required(
                    "phase_1_max_ampere",
                    default=phase_defaults.get("phase_1_max_ampere", 25),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=1,
                        max=63,
                        step=1,
                        unit_of_measurement="A",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    "phase_2_sensor",
                    default=phase_defaults.get("phase_2_sensor", vol.UNDEFINED),
                ): EntitySelector(EntitySelectorConfig(domain="sensor")),
                vol.Optional(
                    "phase_2_max_ampere",
                    default=phase_defaults.get("phase_2_max_ampere", 25),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=1,
                        max=63,
                        step=1,
                        unit_of_measurement="A",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    "phase_3_sensor",
                    default=phase_defaults.get("phase_3_sensor", vol.UNDEFINED),
                ): EntitySelector(EntitySelectorConfig(domain="sensor")),
                vol.Optional(
                    "phase_3_max_ampere",
                    default=phase_defaults.get("phase_3_max_ampere", 25),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=1,
                        max=63,
                        step=1,
                        unit_of_measurement="A",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="phases",
            data_schema=schema,
            errors=errors,
            description_placeholders={"summary": summary},
        )

    async def async_step_params(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Steg 5: Beräkningsparametrar."""
        errors: dict[str, str] = {}

        if user_input is not None:
            min_c = int(user_input[CONF_MIN_CURRENT])
            max_c = int(user_input[CONF_MAX_CURRENT])

            if max_c < min_c:
                errors[CONF_MAX_CURRENT] = "max_less_than_min"
            else:
                self._data[CONF_SAFETY_MARGIN] = int(user_input[CONF_SAFETY_MARGIN])
                self._data[CONF_MIN_CURRENT] = min_c
                self._data[CONF_MAX_CURRENT] = max_c
                self._data[CONF_PHASE_COUNT] = user_input[CONF_PHASE_COUNT]
                return await self.async_step_confirm()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SAFETY_MARGIN,
                    default=self._data.get(CONF_SAFETY_MARGIN, DEFAULT_SAFETY_MARGIN),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=32,
                        step=1,
                        unit_of_measurement="A",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_MIN_CURRENT,
                    default=self._data.get(CONF_MIN_CURRENT, DEFAULT_MIN_CURRENT),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=6,
                        max=32,
                        step=1,
                        unit_of_measurement="A",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_MAX_CURRENT,
                    default=self._data.get(CONF_MAX_CURRENT, DEFAULT_MAX_CURRENT),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=6,
                        max=63,
                        step=1,
                        unit_of_measurement="A",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_PHASE_COUNT,
                    default=self._data.get(CONF_PHASE_COUNT, DEFAULT_PHASE_COUNT),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=["auto", "1", "3"],
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key="phase_count",
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="params",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Steg 6: Bekräftelse och skapande av config entry."""
        if user_input is not None:
            return self.async_create_entry(
                title="EV Load Balancer",
                data=self._data,
            )

        # Bygg summering för description_placeholders
        phases = self._data.get(CONF_PHASES, [])
        profile_id = self._data.get(CONF_PROFILE_ID, "")
        profile_name = PROFILES.get(profile_id, None)
        profile_label = profile_name.name if profile_name else profile_id
        summary_lines = [
            f"Profil: {profile_label}",
            f"Faser: {len(phases)}",
            f"Min: {self._data.get(CONF_MIN_CURRENT, DEFAULT_MIN_CURRENT)} A",
            f"Max: {self._data.get(CONF_MAX_CURRENT, DEFAULT_MAX_CURRENT)} A",
            f"Marginal: {self._data.get(CONF_SAFETY_MARGIN, DEFAULT_SAFETY_MARGIN)} A",
        ]
        summary = "\n".join(summary_lines)

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            last_step=True,
            description_placeholders={"summary": summary},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Returnera Options Flow för efterkonfiguration."""
        return EVLoadBalancerOptionsFlow()


class EVLoadBalancerOptionsFlow(OptionsFlow):
    """Options Flow för EV Load Balancer.

    Tillåter ändring av fassensorer (steg 4) och beräkningsparametrar (steg 5)
    utan att behöva ta bort och återinstallera integrationen.
    """

    def __init__(self) -> None:
        """Initialisera options flow."""
        self._phases: list[dict[str, Any]] = []

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ingångspunkt — gå direkt till fas 4 (fassensorer)."""
        return await self.async_step_phases()

    async def async_step_phases(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Steg 4: Ändra fassensorer (identisk validering som ConfigFlow)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            sensors = [
                v
                for k, v in user_input.items()
                if k.endswith("_sensor") and v is not None and v != ""
            ]

            if len(sensors) == 0:
                errors["base"] = "no_phases"
            elif len(sensors) != len(set(sensors)):
                errors["base"] = "duplicate_phase_sensors"
            else:
                phases = []
                for i in range(1, 4):
                    sensor = user_input.get(f"phase_{i}_sensor")
                    max_a = user_input.get(f"phase_{i}_max_ampere")
                    if sensor is not None and sensor != "":
                        phases.append(
                            {
                                "sensor": sensor,
                                "max_ampere": int(max_a) if max_a is not None else 25,
                                "label": f"L{i}",
                            }
                        )
                # Spara tillfälligt — slås ihop med params i async_step_params
                self._phases = phases
                return await self.async_step_params()

        # Pre-fill från befintliga options eller data
        existing_options = self.config_entry.options
        existing_data = self.config_entry.data
        existing_phases = existing_options.get(CONF_PHASES, existing_data.get(CONF_PHASES, []))

        phase_defaults: dict[str, Any] = {}
        for idx, phase in enumerate(existing_phases[:3], start=1):
            phase_defaults[f"phase_{idx}_sensor"] = phase.get("sensor", "")
            phase_defaults[f"phase_{idx}_max_ampere"] = phase.get("max_ampere", 25)

        # Summering för description_placeholders
        summary = f"{len(existing_phases)} fas(er) konfigurerade"

        schema = vol.Schema(
            {
                vol.Required(
                    "phase_1_sensor",
                    default=phase_defaults.get("phase_1_sensor", vol.UNDEFINED),
                ): EntitySelector(EntitySelectorConfig(domain="sensor")),
                vol.Required(
                    "phase_1_max_ampere",
                    default=phase_defaults.get("phase_1_max_ampere", 25),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=1,
                        max=63,
                        step=1,
                        unit_of_measurement="A",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    "phase_2_sensor",
                    default=phase_defaults.get("phase_2_sensor", vol.UNDEFINED),
                ): EntitySelector(EntitySelectorConfig(domain="sensor")),
                vol.Optional(
                    "phase_2_max_ampere",
                    default=phase_defaults.get("phase_2_max_ampere", 25),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=1,
                        max=63,
                        step=1,
                        unit_of_measurement="A",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    "phase_3_sensor",
                    default=phase_defaults.get("phase_3_sensor", vol.UNDEFINED),
                ): EntitySelector(EntitySelectorConfig(domain="sensor")),
                vol.Optional(
                    "phase_3_max_ampere",
                    default=phase_defaults.get("phase_3_max_ampere", 25),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=1,
                        max=63,
                        step=1,
                        unit_of_measurement="A",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="phases",
            data_schema=schema,
            errors=errors,
            description_placeholders={"summary": summary},
        )

    async def async_step_params(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Steg 5: Ändra beräkningsparametrar."""
        errors: dict[str, str] = {}

        # Pre-fill från befintliga options eller data
        existing_options = self.config_entry.options
        existing_data = self.config_entry.data

        if user_input is not None:
            min_c = int(user_input[CONF_MIN_CURRENT])
            max_c = int(user_input[CONF_MAX_CURRENT])

            if max_c < min_c:
                errors[CONF_MAX_CURRENT] = "max_less_than_min"
            else:
                # Slå ihop phases (från föregående steg) och parametrar
                return self.async_create_entry(
                    data={
                        CONF_PHASES: self._phases,
                        CONF_SAFETY_MARGIN: int(user_input[CONF_SAFETY_MARGIN]),
                        CONF_MIN_CURRENT: min_c,
                        CONF_MAX_CURRENT: max_c,
                        CONF_PHASE_COUNT: user_input[CONF_PHASE_COUNT],
                        CONF_CAPACITY_WARNING_THRESHOLD: int(
                            user_input[CONF_CAPACITY_WARNING_THRESHOLD]
                        ),
                    }
                )

        # Defaults: options > data > systemdefault
        default_safety = existing_options.get(
            CONF_SAFETY_MARGIN,
            existing_data.get(CONF_SAFETY_MARGIN, DEFAULT_SAFETY_MARGIN),
        )
        default_min = existing_options.get(
            CONF_MIN_CURRENT,
            existing_data.get(CONF_MIN_CURRENT, DEFAULT_MIN_CURRENT),
        )
        default_max = existing_options.get(
            CONF_MAX_CURRENT,
            existing_data.get(CONF_MAX_CURRENT, DEFAULT_MAX_CURRENT),
        )
        default_phase_count = existing_options.get(
            CONF_PHASE_COUNT,
            existing_data.get(CONF_PHASE_COUNT, DEFAULT_PHASE_COUNT),
        )
        default_capacity_warning = existing_options.get(
            CONF_CAPACITY_WARNING_THRESHOLD,
            existing_data.get(CONF_CAPACITY_WARNING_THRESHOLD, DEFAULT_CAPACITY_WARNING_THRESHOLD),
        )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SAFETY_MARGIN,
                    default=default_safety,
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=32,
                        step=1,
                        unit_of_measurement="A",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_MIN_CURRENT,
                    default=default_min,
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=6,
                        max=32,
                        step=1,
                        unit_of_measurement="A",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_MAX_CURRENT,
                    default=default_max,
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=6,
                        max=63,
                        step=1,
                        unit_of_measurement="A",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_PHASE_COUNT,
                    default=default_phase_count,
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=["auto", "1", "3"],
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key="phase_count",
                    )
                ),
                vol.Required(
                    CONF_CAPACITY_WARNING_THRESHOLD,
                    default=default_capacity_warning,
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=1,
                        max=10,
                        step=1,
                        unit_of_measurement="A",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="params",
            data_schema=schema,
            errors=errors,
        )
