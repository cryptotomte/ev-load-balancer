"""Sensor-plattform för EV Load Balancer.

Exponerar 7 HA-sensorentiteter:
  - ev_load_balancer_status        : Systemstatus (BalancerState)
  - ev_load_balancer_available_l1  : Tillgänglig ström L1 (A)
  - ev_load_balancer_available_l2  : Tillgänglig ström L2 (A)
  - ev_load_balancer_available_l3  : Tillgänglig ström L3 (A)
  - ev_load_balancer_available_min : Minsta tillgängliga ström (A)
  - ev_load_balancer_target_current: Beräknad målström (A)
  - ev_load_balancer_utilization   : Kapacitetsutnyttjande i procent (%)

Koordinatorn (EVLoadBalancerCoordinator) äger state machine, beräkningsmotor
och lyssnarlista. Alla sensorer prenumererar via koordinatorn.

Events skickas till HA:s event bus vid viktiga tillståndsändringar (PR-07).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfElectricCurrent
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import start as ha_start
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util.dt import utcnow

from .calculator import CalculationResult, calculate
from .charger_profiles import PROFILES
from .command_dispatcher import CommandDispatcher
from .const import (
    CONF_ACTION_ON_SENSOR_LOSS,
    CONF_CAPACITY_WARNING_THRESHOLD,
    CONF_CHARGER_ENTITIES,
    CONF_MAX_CURRENT,
    CONF_MIN_CURRENT,
    CONF_PHASES,
    CONF_PROFILE_ID,
    CONF_SAFE_DEFAULT_CURRENT,
    CONF_SAFETY_MARGIN,
    COOLDOWN_SECONDS,
    DEFAULT_CAPACITY_WARNING_THRESHOLD,
    DEFAULT_MAX_CURRENT,
    DEFAULT_MIN_CURRENT,
    DEFAULT_SAFE_CURRENT,
    DEFAULT_SAFETY_MARGIN,
    DEFAULT_SENSOR_LOSS_ACTION,
    DOMAIN,
    EVENT_CAPACITY_WARNING,
    EVENT_CURRENT_ADJUSTED,
    EVENT_DEVICE_PAUSED,
    EVENT_DEVICE_RESUMED,
    EVENT_FAILSAFE_ACTIVATED,
    EVENT_PHASE_SWITCHED,
    EVENT_SENSOR_LOST,
    PAUSE_DELAY_SECONDS,
    PSM_VALUE_1PHASE,
    PSM_VALUE_3PHASE,
    PSM_VALUE_AUTO,
    RESUME_DELAY_SECONDS,
    RESUME_THRESHOLD_OFFSET,
    SENSOR_AVAILABLE_L1,
    SENSOR_AVAILABLE_L2,
    SENSOR_AVAILABLE_L3,
    SENSOR_AVAILABLE_MIN,
    SENSOR_STATUS,
    SENSOR_TARGET_CURRENT,
    SENSOR_UTILIZATION,
)
from .hysteresis import HysteresisAction, HysteresisController
from .phase_switcher import PhaseMode, PhaseSwitcher
from .state_machine import BalancerState, LoadBalancerStateMachine

_LOGGER = logging.getLogger(__name__)

# Bilstatus som indikerar att bil INTE är ansluten
_CAR_IDLE_STATE = "Idle"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Sätt upp sensor-plattformen för en config entry.

    Skapar koordinatorn, lagrar den i hass.data och registrerar
    6 sensorentiteter.
    """
    coordinator = EVLoadBalancerCoordinator(hass, entry)
    hass.data[DOMAIN][entry.entry_id]["coordinator"] = coordinator
    await coordinator.async_setup()

    async_add_entities(
        [
            BalancerStatusSensor(coordinator),
            AvailableCurrentSensor(coordinator, "l1"),
            AvailableCurrentSensor(coordinator, "l2"),
            AvailableCurrentSensor(coordinator, "l3"),
            AvailableCurrentSensor(coordinator, "min"),
            TargetCurrentSensor(coordinator),
            UtilizationSensor(coordinator),
        ]
    )


class EVLoadBalancerCoordinator:
    """Koordinator som äger state machine, beräkningsmotor och sensor-lyssnare.

    Ansvarig för:
    - Registrering av state change-lyssnare för bevakade HA-entiteter
    - Event-driven beräkningscykel med cooldown (Debouncer)
    - Omedelbar nedreglering utan cooldown (preview-beräkning)
    - Notifiering av registrerade sensorlyssnare efter beräkning
    - Tillståndsövergångar via LoadBalancerStateMachine
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialisera koordinatorn.

        Läser konfiguration med options > data-prioritering.
        """
        self.hass = hass
        self.entry = entry

        # Läs konfiguration med options > data-prioritering
        opts = entry.options
        data = entry.data
        self._phases: list[dict] = opts.get(CONF_PHASES, data.get(CONF_PHASES, []))
        self._charger_entities: dict[str, str] = opts.get(
            CONF_CHARGER_ENTITIES, data.get(CONF_CHARGER_ENTITIES, {})
        )
        self._safety_margin: float = float(
            opts.get(CONF_SAFETY_MARGIN, data.get(CONF_SAFETY_MARGIN, DEFAULT_SAFETY_MARGIN))
        )
        self._min_current: int = int(
            opts.get(CONF_MIN_CURRENT, data.get(CONF_MIN_CURRENT, DEFAULT_MIN_CURRENT))
        )
        self._max_current: int = int(
            opts.get(CONF_MAX_CURRENT, data.get(CONF_MAX_CURRENT, DEFAULT_MAX_CURRENT))
        )
        self._profile_id: str = data.get(CONF_PROFILE_ID, "generic")

        # Failsafe-konfiguration (PR-05), options > data-prioritering
        self._action_on_sensor_loss: str = opts.get(
            CONF_ACTION_ON_SENSOR_LOSS,
            data.get(CONF_ACTION_ON_SENSOR_LOSS, DEFAULT_SENSOR_LOSS_ACTION),
        )
        self._safe_default_current: int = int(
            opts.get(
                CONF_SAFE_DEFAULT_CURRENT,
                data.get(CONF_SAFE_DEFAULT_CURRENT, DEFAULT_SAFE_CURRENT),
            )
        )

        # State machine och senaste beräkningsresultat
        self._state_machine = LoadBalancerStateMachine()
        self.last_result: CalculationResult | None = None
        self._current_target: int = self._min_current
        self._pause_reason: str | None = None

        # Senast bekräftad laddström (används av hysteres-kontrollern)
        self._last_sent_amp: int = self._min_current

        # Hysteres-kontroller (PR-04)
        self._hysteresis = HysteresisController(
            min_current=self._min_current,
            resume_threshold_offset=RESUME_THRESHOLD_OFFSET,
            pause_delay=PAUSE_DELAY_SECONDS,
            resume_delay=RESUME_DELAY_SECONDS,
            cooldown=COOLDOWN_SECONDS,
        )

        # Fas-switcher (PR-06): hanterar automatisk 1↔3-fas växling
        self._phase_switcher = PhaseSwitcher(min_current=self._min_current)

        # Kontrollera om laddarens profil stödjer fasväxling
        profile = PROFILES.get(self._profile_id)
        self._supports_phase_switching = (
            profile is not None and "phase_switching" in profile.capabilities
        )

        # Kommando-dispatcher (PR-04)
        self._dispatcher = CommandDispatcher(hass, self._charger_entities)

        # Lyssnarlista för sensor-callbacks
        self._notify_listeners: list[Callable] = []

        # Debouncer för uppåtreglering (trailing edge, 5s cooldown)
        self._debouncer = Debouncer(
            hass,
            _LOGGER,
            cooldown=COOLDOWN_SECONDS,
            immediate=False,
            function=self._async_calculate,
        )

        # Kapacitetsvarnings-state (för transition-detektion)
        self._last_capacity_warning: bool = False

        # Cleanup-callbacks (registrerade lyssnare)
        self._remove_listeners: list[Callable] = []

    @property
    def state(self) -> BalancerState:
        """Returnerar aktuellt state machine-tillstånd."""
        return self._state_machine.state

    @property
    def pause_reason(self) -> str | None:
        """Returnerar orsak till PAUSED-tillstånd, eller None."""
        return self._pause_reason

    @property
    def last_sent_amp(self) -> int:
        """Returnerar senast bekräftad laddström i ampere."""
        return self._last_sent_amp

    @property
    def phases(self) -> list[dict]:
        """Returnerar lista med konfigurerade faser (publik property)."""
        return self._phases

    @property
    def phase_switcher_mode(self) -> PhaseMode:
        """Returnerar fasväxlarens aktuella läge (three_phase/one_phase)."""
        return self._phase_switcher.current_mode

    def register_listener(self, callback_fn: Callable) -> None:
        """Registrera en sensor-lyssnare som anropas vid uppdateringar."""
        self._notify_listeners.append(callback_fn)

    def unregister_listener(self, callback_fn: Callable) -> None:
        """Avregistrera en sensor-lyssnare."""
        if callback_fn in self._notify_listeners:
            self._notify_listeners.remove(callback_fn)

    def _notify_all_listeners(self) -> None:
        """Notifiera alla registrerade lyssnare om uppdatering."""
        for listener in self._notify_listeners:
            listener()

    def _fire_event(self, event_type: str, data: dict) -> None:
        """Skicka ett event till HA:s event bus.

        Lägger automatiskt till entry_id och ISO 8601-tidsstämpel i event-data.
        hass.bus.async_fire är synkron och kastar inga undantag vid normal drift.

        Args:
            event_type: Event-typ, t.ex. EVENT_CURRENT_ADJUSTED.
            data: Extra kontext-data att inkludera i eventet.
        """
        event_data = {
            "entry_id": self.entry.entry_id,
            "timestamp": utcnow().isoformat(),
            **data,
        }
        self.hass.bus.async_fire(event_type, event_data)

    def _get_capacity_warning_threshold(self) -> int:
        """Läs kapacitetsvarnings-tröskel med options > data > default-prioritering."""
        opts = self.entry.options
        data = self.entry.data
        raw = opts.get(
            CONF_CAPACITY_WARNING_THRESHOLD,
            data.get(CONF_CAPACITY_WARNING_THRESHOLD, DEFAULT_CAPACITY_WARNING_THRESHOLD),
        )
        try:
            return int(raw)
        except (ValueError, TypeError):
            _LOGGER.warning(
                "Ogiltigt värde för %s: %r — faller tillbaka till default %sA",
                CONF_CAPACITY_WARNING_THRESHOLD,
                raw,
                DEFAULT_CAPACITY_WARNING_THRESHOLD,
            )
            return DEFAULT_CAPACITY_WARNING_THRESHOLD

    def _check_capacity_warning(self, result: CalculationResult) -> None:
        """Kontrollera kapacitetsvarning och skicka event vid tillståndsövergång.

        Detekterar övergångar off→on och on→off och loggar/skickar event.

        Args:
            result: Senaste beräkningsresultat.
        """
        threshold = self._get_capacity_warning_threshold()
        current_warning = result.available_min < threshold

        if current_warning and not self._last_capacity_warning:
            # Övergång off→on: kapacitetsvarning aktiv
            _LOGGER.warning(
                "Kapacitetsvarning: available_min=%.1fA < tröskel=%sA",
                result.available_min,
                threshold,
            )
            self._fire_event(
                EVENT_CAPACITY_WARNING,
                {
                    "available_min": result.available_min,
                    "threshold": threshold,
                    "phase_loads": result.phase_loads,
                    "active": True,
                },
            )
        elif not current_warning and self._last_capacity_warning:
            # Övergång on→off: kapacitetsvarning upphävd
            _LOGGER.info(
                "Kapacitetsvarning upphävd: available_min=%.1fA >= tröskel=%sA",
                result.available_min,
                threshold,
            )
            self._fire_event(
                EVENT_CAPACITY_WARNING,
                {
                    "available_min": result.available_min,
                    "threshold": threshold,
                    "phase_loads": list(result.phase_loads),
                    "active": False,
                },
            )

        self._last_capacity_warning = current_warning

    def fire_sensor_lost_event(self, sensor_entity: str, action_taken: str) -> None:
        """Skicka event för sensorförlust.

        Anropas externt (t.ex. vid failsafe-hantering) när en sensor
        blir unavailable och en åtgärd vidtagits.

        Args:
            sensor_entity: Entitets-ID för sensorn som försvann.
            action_taken: Beskrivning av vidtagen åtgärd.
        """
        # TODO: PR-05 — anslut hit från _async_handle_sensor_unavailable
        _LOGGER.warning(
            "Sensorförlust: %s — åtgärd: %s",
            sensor_entity,
            action_taken,
        )
        self._fire_event(
            EVENT_SENSOR_LOST,
            {
                "sensor_entity": sensor_entity,
                "action_taken": action_taken,
            },
        )

    def fire_failsafe_activated_event(
        self, trigger: str, action: str, sensors_status: dict
    ) -> None:
        """Skicka event för failsafe-aktivering.

        Anropas när systemet går in i FAILSAFE-tillstånd.

        Args:
            trigger: Orsak till failsafe-aktivering.
            action: Vidtagen åtgärd.
            sensors_status: Dict med sensorernas status.
        """
        # TODO: PR-05 — anslut hit från failsafe-logiken
        _LOGGER.error(
            "Failsafe aktiverad: trigger=%s, åtgärd=%s",
            trigger,
            action,
        )
        self._fire_event(
            EVENT_FAILSAFE_ACTIVATED,
            {
                "trigger": trigger,
                "action": action,
                "sensors_status": sensors_status,
            },
        )

    def fire_phase_switched_event(
        self,
        from_mode: str,
        to_mode: str,
        reason: str,
        available_per_phase: list[float],
    ) -> None:
        """Skicka event för fasväxling.

        Anropas när systemet växlar mellan 1-fas och 3-fas.

        Args:
            from_mode: Föregående fasläge ('1-phase' eller '3-phase').
            to_mode: Nytt fasläge ('1-phase' eller '3-phase').
            reason: Orsak till fasväxlingen.
            available_per_phase: Tillgänglig ström per fas (A).
        """
        _LOGGER.info(
            "Fasväxling: %s → %s (%s)",
            from_mode,
            to_mode,
            reason,
        )
        self._fire_event(
            EVENT_PHASE_SWITCHED,
            {
                "from_mode": from_mode,
                "to_mode": to_mode,
                "reason": reason,
                "available_per_phase": available_per_phase,
            },
        )

    async def async_setup(self) -> None:
        """Registrera event-lyssnare för alla bevakade entiteter.

        Bevakade entiteter:
        - Hushållets fassensorer (phases[i]["sensor"])
        - Bilstatus (car_value)
        - Faskarta (map)
        - Laddarens ström per fas (nrg_4, nrg_5, nrg_6)
        """

        # Skjut upp PSM auto + initial fasläge till efter att HA är fullt uppstartat.
        # select.select_option-tjänsten är ej tillgänglig under setup (timing-issue).
        async def _init_after_ha_start(_hass: HomeAssistant) -> None:
            psm_sent = await self._dispatcher.send_psm(PSM_VALUE_AUTO)
            if psm_sent:
                _LOGGER.info("PSM satt till auto (0) vid uppstart")
            else:
                _LOGGER.warning(
                    "Kunde inte sätta PSM auto vid uppstart — psm-entitet saknas/unavailable"
                )
            initial_phases = self._read_active_phases_sync()
            self._phase_switcher.set_initial_mode(initial_phases)
            _LOGGER.debug("Initial fasläge satt baserat på: %s", initial_phases)

        ha_start.async_at_started(self.hass, _init_after_ha_start)

        # Samla alla entitets-ID:n att bevaka
        entities_to_track: list[str] = []

        for phase in self._phases:
            sensor_id = phase.get("sensor", "")
            if sensor_id:
                entities_to_track.append(sensor_id)

        # Laddar-entiteter att bevaka
        for key in ("car_value", "map", "pha", "nrg_4", "nrg_5", "nrg_6"):
            entity_id = self._charger_entities.get(key, "")
            if entity_id:
                entities_to_track.append(entity_id)

        if entities_to_track:
            remove_fn = async_track_state_change_event(
                self.hass,
                entities_to_track,
                self._handle_state_change,
            )
            self._remove_listeners.append(remove_fn)

        # Kör initial beräkning direkt (schemalägger via debouncer)
        self._debouncer.async_schedule_call()

    async def async_shutdown(self) -> None:
        """Avregistrera alla lyssnare och avbryt debouncer vid shutdown."""
        self._debouncer.async_cancel()
        for remove_fn in self._remove_listeners:
            remove_fn()
        self._remove_listeners.clear()
        self._notify_listeners.clear()

    @callback
    def _handle_state_change(self, event: Event) -> None:
        """Hanterar state change-event för bevakade entiteter.

        Synkron callback (körs i event loop).

        Logik:
        - Kontrollera om en bevakad fassensor blivit unavailable → failsafe
        - Kontrollera om alla fassensorer är tillgängliga igen → återhämtning
        - Beräkna preview (snabb synkron förhandsberäkning)
        - Om preview < current_target: nedreglering → avbryt debouncer + direkt task
        - Annars: uppåtreglering → schemalägg via debouncer (cooldown 5s)
        """
        # Hämta entitets-ID:n för ändrade sensorer
        entity_id = event.data.get("entity_id", "")
        new_state = event.data.get("new_state")

        # Bygg lista med fas-sensor-ID:n
        phase_sensor_ids = {
            phase.get("sensor", "") for phase in self._phases if phase.get("sensor", "")
        }

        # Kontrollera om en fassensor ändrade status
        if entity_id in phase_sensor_ids:
            if new_state is not None and new_state.state == "unavailable":
                # Fassensor blev unavailable — kontrollera om alla är borta eller bara en
                self._debouncer.async_cancel()
                self.hass.async_create_task(self._async_handle_sensor_unavailable())
                return

            if new_state is not None and new_state.state not in ("unavailable", "unknown"):
                # En fassensor blev tillgänglig igen — kontrollera om vi är i FAILSAFE
                if self._state_machine.state == BalancerState.FAILSAFE:
                    self._debouncer.async_cancel()
                    self.hass.async_create_task(self._async_check_recovery())
                    return

        # Normal logik: preview-beräkning för nedreglering/uppåtreglering
        # Hoppa över om vi är i FAILSAFE (ingen normal beräkning under failsafe)
        if self._state_machine.state == BalancerState.FAILSAFE:
            return

        preview = self._calculate_preview()

        if preview is not None and preview < self._current_target:
            # Nedreglering: avbryt cooldown och kör omedelbart (US3)
            _LOGGER.debug(
                "Nedreglering detekterad: preview=%sA < current_target=%sA — kör omedelbart",
                preview,
                self._current_target,
            )
            self._debouncer.async_cancel()
            self.hass.async_create_task(self._async_calculate())
        else:
            # Uppåtreglering eller oförändrat: vänta på cooldown (US3)
            self._debouncer.async_schedule_call()

    async def _async_handle_sensor_unavailable(self) -> None:
        """Hanterar sensorförlust — avgör om total eller enskild, triggar failsafe.

        Räknar unavailable fassensorer och väljer åtgärd:
        - Alla borta: alltid paus (frc='1'), logga CRITICAL
        - Enskild: action='reduce' → send_amp(safe_default), action='pause' → pause()
        """
        # Räkna unavailable fassensorer
        unavailable_count = 0
        total_count = 0
        for phase in self._phases:
            sensor_id = phase.get("sensor", "")
            if not sensor_id:
                continue
            total_count += 1
            state = self.hass.states.get(sensor_id)
            if state is None or state.state in ("unavailable", "unknown"):
                unavailable_count += 1

        if unavailable_count == 0:
            # Ingen sensor unavailable längre — ingenting att göra
            return

        # Hämta aktuellt state machine-tillstånd INNAN övergång
        current_sm_state = self._state_machine.state

        # Undvik dubbel-enter av FAILSAFE
        if current_sm_state == BalancerState.FAILSAFE:
            return

        # Failsafe ej tillämpligt under INITIALIZING (sensorer ej stabila ännu)
        if current_sm_state == BalancerState.INITIALIZING:
            return

        if unavailable_count == total_count:
            # Total sensorförlust — alltid pausa, oavsett konfiguration
            _LOGGER.critical(
                "FAILSAFE: Alla %d fassensorer är unavailable — laddning stoppas omedelbart",
                total_count,
            )
            self._state_machine.enter_failsafe(current_sm_state)
            self._pause_reason = "sensor_unavailable"
            await self._dispatcher.pause()
        else:
            # Enskild sensorförlust — följ konfigurerad action
            _LOGGER.error(
                "FAILSAFE: %d av %d fassensorer är unavailable — åtgärd: %s",
                unavailable_count,
                total_count,
                self._action_on_sensor_loss,
            )
            self._state_machine.enter_failsafe(current_sm_state)
            self._pause_reason = "sensor_unavailable"

            if self._action_on_sensor_loss == "pause":
                await self._dispatcher.pause()
            else:
                # Default: reduce till safe_default_current
                await self._dispatcher.send_amp(self._safe_default_current)

        self._notify_all_listeners()

    async def _async_check_recovery(self) -> None:
        """Kontrollera om alla fassensorer är tillgängliga igen för återhämtning.

        Anropas när en sensor som var unavailable blir tillgänglig igen.
        Om alla sensorer nu är OK: anropa recover_from_failsafe() och
        starta ny beräkningscykel.
        """
        if self._state_machine.state != BalancerState.FAILSAFE:
            return

        # Kontrollera att ALLA fassensorer är tillgängliga
        for phase in self._phases:
            sensor_id = phase.get("sensor", "")
            if not sensor_id:
                continue
            state = self.hass.states.get(sensor_id)
            if state is None or state.state in ("unavailable", "unknown"):
                # Minst en sensor fortfarande unavailable — vänta
                return

        # Alla sensorer tillgängliga — återhämtning
        _LOGGER.info(
            "FAILSAFE-återhämtning: alla fassensorer tillgängliga igen — återgår till normalt läge"
        )
        self._state_machine.recover_from_failsafe()
        self._pause_reason = None
        self._notify_all_listeners()

        # Starta beräkningscykel för att återta normal drift
        self._debouncer.async_schedule_call()

    def _calculate_preview(self) -> int | None:
        """Snabb synkron förhandsberäkning baserat på aktuella HA-states.

        Returnerar beräknat target_current, eller None om sensorer saknas.
        Används för att avgöra om nedreglering behövs utan full async-beräkning.
        """
        if not self._phases:
            return None

        # Läs fassensorvärden synkront
        phase_values: list[float] = []
        for phase in self._phases:
            sensor_id = phase.get("sensor", "")
            if not sensor_id:
                return None
            state = self.hass.states.get(sensor_id)
            if state is None or state.state in ("unavailable", "unknown"):
                return None
            try:
                phase_values.append(float(state.state))
            except (ValueError, TypeError):
                return None

        # Läs device-värden synkront (0.0 om saknas)
        device_values = self._read_device_values_sync()

        # Parsa map för aktiva faser
        active_phase_numbers = self._read_active_phases_sync()

        # Snabb beräkning
        try:
            result = calculate(
                phases=self._phases,
                phase_values=phase_values,
                device_values=device_values,
                active_phase_numbers=active_phase_numbers,
                safety_margin=self._safety_margin,
                min_current=self._min_current,
                max_current=self._max_current,
            )
            return result.target_current
        except (ValueError, TypeError, KeyError, IndexError) as err:
            _LOGGER.debug(
                "Preview-beräkning misslyckades: %s — faller tillbaka till debouncer",
                err,
            )
            return None

    def _read_device_values_sync(self) -> list[float]:
        """Läs laddarens ström per fas synkront.

        Fallback-logik (PR-05/US3):
        - Om laddarens nrg-sensor är unavailable: använd _last_sent_amp
          som fallback-värde och logga WARNING.
        - Om sensorn saknas helt: använd 0.0A som tidigare.
        """
        device_values: list[float] = []
        for _, key in zip(self._phases, ("nrg_4", "nrg_5", "nrg_6")):
            entity_id = self._charger_entities.get(key, "")
            if entity_id:
                state = self.hass.states.get(entity_id)
                if state and state.state not in ("unavailable", "unknown", ""):
                    try:
                        device_values.append(float(state.state))
                        continue
                    except (ValueError, TypeError):
                        _LOGGER.debug(
                            "Enhetsensor '%s' har ogiltigt värde '%s' — använder 0.0A",
                            entity_id,
                            state.state,
                        )
                elif state and state.state in ("unavailable", "unknown"):
                    # Laddarens sensor unavailable — fallback till senast satta amp-värde
                    _LOGGER.warning(
                        "Laddarens sensor '%s' är %s — använder senast satt ström %sA som fallback",
                        entity_id,
                        state.state,
                        self._last_sent_amp,
                    )
                    device_values.append(float(self._last_sent_amp))
                    continue
            device_values.append(0.0)
        return device_values

    def _read_active_phases_sync(self) -> list[int]:
        """Läs aktiva fasnummer synkront med prioritetsordning: pha → map → fallback.

        Prioritetsordning:
        1. pha-sensor: faktiska faser efter kontaktorn (go-e API, JSON-array)
        2. map-sensor: faskarta som speglar PSM-inställningen
        3. Fallback: alla konfigurerade faser

        Returnerar lista med fasnummer (1-baserat), t.ex. [1] eller [1, 2, 3].
        """
        # --- Prioritet 1: pha-sensor (faktisk fasanvändning efter kontaktorn) ---
        pha_entity = self._charger_entities.get("pha", "")
        if pha_entity:
            state = self.hass.states.get(pha_entity)
            if state and state.state not in ("unavailable", "unknown", ""):
                try:
                    pha = json.loads(state.state)
                    if isinstance(pha, list) and len(pha) >= 3:
                        # pha[0:3] = faser efter kontaktorn (till bilen)
                        active = [i + 1 for i, v in enumerate(pha[0:3]) if v]
                        if active:
                            _LOGGER.debug("Aktiva faser: %s (källa: pha)", active)
                            return active
                except (json.JSONDecodeError, ValueError, TypeError):
                    _LOGGER.warning(
                        "pha-sensor '%s' har ogiltigt värde '%s' — faller tillbaka på map",
                        pha_entity,
                        state.state,
                    )

        # --- Prioritet 2: map-sensor (speglar PSM-inställningen) ---
        map_entity = self._charger_entities.get("map", "")
        if map_entity:
            state = self.hass.states.get(map_entity)
            if state and state.state not in ("unavailable", "unknown", ""):
                try:
                    parsed = json.loads(state.state)
                    if isinstance(parsed, list):
                        active = [int(x) for x in parsed]
                        _LOGGER.debug("Aktiva faser: %s (källa: map)", active)
                        return active
                except (json.JSONDecodeError, ValueError, TypeError):
                    _LOGGER.warning(
                        "Map-sensor '%s' har ogiltigt värde '%s' — faller tillbaka till alla faser",
                        map_entity,
                        state.state,
                    )

        # --- Prioritet 3: Fallback — alla konfigurerade faser ---
        fallback = list(range(1, len(self._phases) + 1))
        _LOGGER.debug("Aktiva faser: %s (källa: fallback)", fallback)
        return fallback

    async def _async_calculate(self) -> None:
        """Asynkron beräkning — läser HA-state, kör calculator, skickar kommandon.

        Flöde:
        1. Om state är FAILSAFE: hoppa över beräkning (failsafe-logik hanteras separat).
        2. Läser fassensorvärden — avbryter om unavailable/unknown (FR-002/C1).
        3. Läser enhetsvärden (nrg_4/5/6) och aktiva faser (map).
        4. Kör calculate() för att beräkna available_* och target_current.
        5. Registrerar lyckad beräkning via state machine (INITIALIZING → IDLE efter 2 st).
        6. Hanterar bilstatus för IDLE/BALANCING-övergångar (car_value).
        7. I BALANCING eller PAUSED: utvärderar hysteres-kontrollern (PR-04) och
           skickar kommandon via CommandDispatcher. Uppdaterar _last_sent_amp och
           anropar record_amp_change() endast om dispatchen lyckades (bool True).
        8. Notifierar alla registrerade sensorlyssnare.
        """
        if not self._phases:
            _LOGGER.warning("Inga fassensorer konfigurerade — beräkning avbryts")
            return

        # FAILSAFE: hoppa över normal beräkning — failsafe-logik hanteras av
        # _async_handle_sensor_unavailable() och _async_check_recovery()
        if self._state_machine.state == BalancerState.FAILSAFE:
            _LOGGER.debug("Beräkning avbryts: systemet är i FAILSAFE-tillstånd")
            return

        # Läs fassensorvärden — kontrollera unavailable
        phase_values: list[float] = []
        for phase in self._phases:
            sensor_id = phase.get("sensor", "")
            if not sensor_id:
                _LOGGER.warning("Fas saknar sensor-ID — beräkning avbryts")
                self.last_result = None
                self._notify_all_listeners()
                return
            state = self.hass.states.get(sensor_id)
            if state is None or state.state in ("unavailable", "unknown"):
                _LOGGER.warning(
                    "Fassensor '%s' är %s — beräkning räknas ej som lyckad (FR-002/C1)",
                    sensor_id,
                    state.state if state else "saknas",
                )
                # Sensorn är unavailable — notifiera lyssnare och returnera
                # Sensorerna returnerar None (unavailable) via last_result=None (FR-020)
                self.last_result = None
                self._notify_all_listeners()
                return
            try:
                phase_values.append(float(state.state))
            except (ValueError, TypeError):
                _LOGGER.warning(
                    "Fassensor '%s' har ogiltigt värde '%s' — beräkning avbryts",
                    sensor_id,
                    state.state,
                )
                self.last_result = None
                self._notify_all_listeners()
                return

        # Läs device-värden (laddarens uttag per fas)
        device_values = self._read_device_values_sync()

        # Parsa map för aktiva faser
        active_phase_numbers = self._read_active_phases_sync()

        # Kör beräkning
        try:
            result = calculate(
                phases=self._phases,
                phase_values=phase_values,
                device_values=device_values,
                active_phase_numbers=active_phase_numbers,
                safety_margin=self._safety_margin,
                min_current=self._min_current,
                max_current=self._max_current,
            )
        except Exception:
            _LOGGER.exception("Beräkningsfel i calculate() — sensorer sätts till unavailable")
            self.last_result = None
            self._notify_all_listeners()
            return

        _LOGGER.debug(
            "Beräkning klar: target=%sA, active_phases=%s, charging_mode=%s",
            result.target_current,
            result.active_phases,
            result.charging_mode,
        )

        # Uppdatera senaste resultat och current target
        self.last_result = result
        self._current_target = result.target_current

        # Registrera lyckad beräkning (alla sensorer tillgängliga)
        prev_state = self._state_machine.state
        transitioned = self._state_machine.record_successful_calculation()
        if transitioned:
            _LOGGER.info(
                "Tillståndsändring: %s → %s (2 lyckade beräkningar)",
                prev_state,
                self._state_machine.state,
            )

        # Hantera bilstatus → IDLE/BALANCING-övergångar
        self._handle_car_status()

        # Detektera PHEV: uppdatera phase_switcher om laddaren enbart kör 1-fas
        if self._supports_phase_switching:
            self._detect_phev_and_update_capability(active_phase_numbers)

        # Hantera fasväxling INNAN hysteres (PR-06)
        # Fasväxling kan lösa kapacitetsbrist utan att behöva pausa (US4)
        current_sm_state = self._state_machine.state
        if self._supports_phase_switching and current_sm_state in (
            BalancerState.BALANCING,
            BalancerState.PAUSED,
        ):
            phase_cmd = self._phase_switcher.evaluate(
                available_per_phase=result.available_per_phase,
                min_current=self._min_current,
                now=utcnow(),
            )
            if phase_cmd is not None:
                psm_value = (
                    PSM_VALUE_1PHASE
                    if phase_cmd.target_mode == PhaseMode.ONE_PHASE
                    else PSM_VALUE_3PHASE
                )
                psm_sent = await self._dispatcher.send_psm(psm_value)
                if psm_sent:
                    self._phase_switcher.record_mode_change(phase_cmd.target_mode)
                    _LOGGER.info(
                        "Fasväxling: %s (psm='%s') — %s",
                        phase_cmd.action,
                        psm_value,
                        phase_cmd.reason,
                    )

        # Hantera kapacitetsbrist/reglering via hysteres (PR-04)
        current_sm_state = self._state_machine.state
        if current_sm_state in (BalancerState.BALANCING, BalancerState.PAUSED):
            is_paused = current_sm_state == BalancerState.PAUSED
            now = utcnow()

            cmd = self._hysteresis.evaluate(
                available_min=result.available_min,
                target_current=result.target_current,
                last_sent_amp=self._last_sent_amp,
                is_paused=is_paused,
                now=now,
            )

            if cmd.action == HysteresisAction.SET_AMP:
                # Reglera laddström (upp eller ned)
                old_amp = self._last_sent_amp
                direction = "sänkt" if cmd.amp < old_amp else "höjd"
                sent = await self._dispatcher.send_amp(cmd.amp)
                if sent:
                    self._last_sent_amp = cmd.amp
                    self._hysteresis.record_amp_change(now)
                    _LOGGER.info(
                        "Ström %s: %sA → %sA (%s)",
                        direction,
                        old_amp,
                        cmd.amp,
                        cmd.reason,
                    )
                    # Skicka event om strömsändning lyckades
                    self._fire_event(
                        EVENT_CURRENT_ADJUSTED,
                        {
                            "old_current": old_amp,
                            "new_current": cmd.amp,
                            "reason": cmd.reason,
                            "phase_loads": result.phase_loads,
                            "available": result.available_min,
                        },
                    )

            elif cmd.action == HysteresisAction.PAUSE:
                # Pausa laddning — övergå till PAUSED
                paused = await self._dispatcher.pause()
                if paused:
                    self._state_machine.on_below_min_current()
                    self._pause_reason = "insufficient_capacity"
                    _LOGGER.warning(
                        "Laddning pausad: %s",
                        cmd.reason,
                    )
                    # Skicka event om pausning lyckades
                    self._fire_event(
                        EVENT_DEVICE_PAUSED,
                        {
                            "reason": cmd.reason,
                            "available_min": result.available_min,
                            "min_current": self._min_current,
                            "phase_loads": result.phase_loads,
                        },
                    )

            elif cmd.action == HysteresisAction.RESUME:
                # Återuppta laddning — övergå till BALANCING
                resumed = await self._dispatcher.resume(cmd.amp)
                if resumed:
                    self._state_machine.on_above_min_current()
                    self._pause_reason = None
                    self._last_sent_amp = cmd.amp
                    self._hysteresis.record_amp_change(now)
                    _LOGGER.info(
                        "Laddning återupptagen: %sA (%s)",
                        cmd.amp,
                        cmd.reason,
                    )
                    # Skicka event om resume lyckades
                    self._fire_event(
                        EVENT_DEVICE_RESUMED,
                        {
                            "new_current": cmd.amp,
                            "available_per_phase": [
                                result.charger_budget_l1,
                                result.charger_budget_l2,
                                result.charger_budget_l3,
                            ],
                        },
                    )

            else:
                # NONE — ingen åtgärd
                _LOGGER.debug(
                    "Hysteres: ingen åtgärd (%s)",
                    cmd.reason,
                )

        # VIKTIGT: kapacitetsvarning kontrolleras FÖRE _notify_all_listeners() för att
        # säkerställa att event skickas på HA:s eventbuss innan binary_sensor:s
        # state skrivs till HA via async_write_ha_state.
        self._check_capacity_warning(result)

        # Notifiera alla sensorlyssnare om uppdatering
        self._notify_all_listeners()

    def _detect_phev_and_update_capability(self, active_phase_numbers: list[int]) -> None:
        """Detektera PHEV och uppdatera fasväxlarens kapabilitet.

        PHEV-logik: Om map-sensorn visar enbart 1-fas (exakt 1 fasnummer) OCH
        fasväxlaren är i THREE_PHASE-läge tolkas det som att laddaren/bilen
        tvingade 1-fas utan att vi kommenderade det (trolig PHEV). I så fall
        inaktiveras fasväxling.

        Om map visar 3-fas (fler än en fas aktiv) återaktiveras fasväxling.

        Vi checkar mot phase_switcher.current_mode för att undvika att
        inaktivera fasväxling när VI själva nyss kommenderade 1-fas.

        Args:
            active_phase_numbers: Aktiva fasnummer från map-sensorn.
        """
        if (
            len(active_phase_numbers) == 1
            and self._phase_switcher.current_mode == PhaseMode.THREE_PHASE
        ):
            # Map visar 1-fas men vi är i 3-fas-läge → PHEV tvingade 1-fas
            self._phase_switcher.set_device_capability(supports_3phase=False)
            _LOGGER.debug(
                "PHEV-detektion: map visar enbart 1 fas %s i 3-fas-läge — fasväxling inaktiverad",
                active_phase_numbers,
            )
        elif len(active_phase_numbers) > 1:
            # Map visar 3-fas → laddaren klarar 3-fas
            self._phase_switcher.set_device_capability(supports_3phase=True)

    def _handle_car_status(self) -> None:
        """Hanterar bilstatus för IDLE/BALANCING-övergångar.

        Läser car_value-sensorn och triggar övergångar i state machine.
        Not: FAILSAFE-tillstånd hanteras inte här (det är en fassensor-händelse).
        """
        car_entity = self._charger_entities.get("car_value", "")
        if not car_entity:
            return

        state = self.hass.states.get(car_entity)
        if state is None or state.state in ("unavailable", "unknown"):
            _LOGGER.debug(
                "Bilsensor '%s' är %s — hoppar över statushantering",
                car_entity,
                state.state if state else "saknas",
            )
            return

        car_state = state.state
        sm_state = self._state_machine.state

        if sm_state == BalancerState.IDLE and car_state != _CAR_IDLE_STATE:
            # Bil ansluten — övergå till BALANCING
            if self._state_machine.on_car_connected():
                _LOGGER.info("Tillståndsändring: IDLE → BALANCING (car_value='%s')", car_state)
        elif (
            sm_state in (BalancerState.BALANCING, BalancerState.PAUSED)
            and car_state == _CAR_IDLE_STATE
        ):
            # Bil bortkopplad — återgå till IDLE
            prev = sm_state
            if self._state_machine.on_car_disconnected():
                self._pause_reason = None
                # Nollställ hysteres-state och last_sent_amp (T008)
                self._hysteresis.reset()
                self._last_sent_amp = self._min_current
                _LOGGER.info("Tillståndsändring: %s → IDLE (car_value='Idle')", prev)


# ---------------------------------------------------------------------------
# Sensor-basklassen
# ---------------------------------------------------------------------------


class _EVSensorBase(SensorEntity):
    """Basklass för EV Load Balancer-sensorer.

    Gemensamma egenskaper:
    - _attr_should_poll = False (event-driven, ingen polling)
    - _attr_has_entity_name = True
    - Registrerar async_write_ha_state som koordinator-lyssnare
    """

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, coordinator: EVLoadBalancerCoordinator) -> None:
        """Initialisera bassensorn."""
        self._coordinator = coordinator

    @property
    def device_info(self) -> DeviceInfo:
        """Returnerar device-info för att koppla sensorn till rätt device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._coordinator.entry.entry_id)},
        )

    async def async_added_to_hass(self) -> None:
        """Registrera lyssnare och cleanup-callback när sensorn läggs till i HA."""
        await super().async_added_to_hass()
        self._coordinator.register_listener(self.async_write_ha_state)
        self.async_on_remove(
            lambda: self._coordinator.unregister_listener(self.async_write_ha_state)
        )


# ---------------------------------------------------------------------------
# Statussensor
# ---------------------------------------------------------------------------


class BalancerStatusSensor(_EVSensorBase):
    """Sensor som visar lastbalanserarens aktuella tillstånd.

    Värde: BalancerState-sträng (initializing/idle/balancing/paused)
    Extra attribut: 10 stycken per FR-019
    """

    _attr_translation_key = SENSOR_STATUS

    def __init__(self, coordinator: EVLoadBalancerCoordinator) -> None:
        """Initialisera statussensorn."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{SENSOR_STATUS}"
        self._attr_name = "Status"

    @property
    def native_value(self) -> str:
        """Returnerar aktuellt tillstånd som sträng."""
        return str(self._coordinator.state)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Returnerar alla status-attribut (FR-019, FR-012).

        Vid FAILSAFE exponeras paused_reason='sensor_unavailable' (FR-012).
        """
        result = self._coordinator.last_result
        entry = self._coordinator.entry
        opts = entry.options
        data = entry.data

        # Hämta safety_margin och profile_id för attribut
        safety_margin = float(
            opts.get(CONF_SAFETY_MARGIN, data.get(CONF_SAFETY_MARGIN, DEFAULT_SAFETY_MARGIN))
        )
        profile_id = data.get(CONF_PROFILE_ID, "generic")

        return {
            "target_current": result.target_current if result else None,
            "pause_reason": self._coordinator.pause_reason,
            "last_calculation": (result.calculation_time.isoformat() if result else None),
            "phase_loads": result.phase_loads if result else None,
            "device_loads": result.device_loads if result else None,
            "active_phases": result.active_phases if result else None,
            "charging_mode": result.charging_mode if result else None,
            "phase_mode": str(self._coordinator.phase_switcher_mode),
            "safety_margin": safety_margin,
            "charger_profile": profile_id,
            "last_sent_amp": self._coordinator.last_sent_amp,
        }


# ---------------------------------------------------------------------------
# Tillgänglig ström per fas
# ---------------------------------------------------------------------------


class AvailableCurrentSensor(_EVSensorBase):
    """Sensor som visar tillgänglig ström för en specifik fas (eller minimum).

    Stöder faserna: l1, l2, l3, min
    """

    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE

    # Mappning fas-etikett → attributnamn i CalculationResult
    # Visar fuse_headroom (faktisk säkringsmarginal) — inte charger_budget
    _PHASE_ATTR: dict[str, str] = {
        "l1": "fuse_headroom_l1",
        "l2": "fuse_headroom_l2",
        "l3": "fuse_headroom_l3",
        "min": "fuse_headroom_min",
    }

    def __init__(
        self,
        coordinator: EVLoadBalancerCoordinator,
        phase: str,
    ) -> None:
        """Initialisera fasstromsensorn.

        Args:
            coordinator: Koordinatorn som äger beräkningsdata.
            phase: En av 'l1', 'l2', 'l3', 'min'.
        """
        super().__init__(coordinator)
        self._phase = phase

        # Välj rätt konstant för sensor-suffix
        suffix_map = {
            "l1": SENSOR_AVAILABLE_L1,
            "l2": SENSOR_AVAILABLE_L2,
            "l3": SENSOR_AVAILABLE_L3,
            "min": SENSOR_AVAILABLE_MIN,
        }
        suffix = suffix_map[phase]
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{suffix}"
        self._attr_name = f"Available {phase.upper()}"
        self._attr_translation_key = suffix

    @property
    def native_value(self) -> float | None:
        """Returnerar tillgänglig ström för fasen, eller None om ej beräknad."""
        result = self._coordinator.last_result
        if result is None:
            return None
        return getattr(result, self._PHASE_ATTR[self._phase], None)


# ---------------------------------------------------------------------------
# Beräknad målström
# ---------------------------------------------------------------------------


class TargetCurrentSensor(_EVSensorBase):
    """Sensor som visar beräknad optimal laddström.

    Värde: int i ampere, eller None om ingen beräkning är klar.
    """

    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_translation_key = SENSOR_TARGET_CURRENT

    def __init__(self, coordinator: EVLoadBalancerCoordinator) -> None:
        """Initialisera målströmsensorn."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{SENSOR_TARGET_CURRENT}"
        self._attr_name = "Target Current"

    @property
    def native_value(self) -> int | None:
        """Returnerar beräknad målström i ampere, eller None."""
        result = self._coordinator.last_result
        if result is None:
            return None
        return result.target_current


# ---------------------------------------------------------------------------
# Kapacitetsutnyttjande (utilization)
# ---------------------------------------------------------------------------


class UtilizationSensor(_EVSensorBase):
    """Sensor som visar kapacitetsutnyttjande i procent.

    Beräknas som: (max_ampere - available_min) / max_ampere * 100
    Kläms till intervallet [0, 100].
    Visas som unavailable om max_ampere är 0 eller om ingen beräkning är klar.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_translation_key = SENSOR_UTILIZATION

    def __init__(self, coordinator: EVLoadBalancerCoordinator) -> None:
        """Initialisera utilization-sensorn."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{SENSOR_UTILIZATION}"
        self._attr_name = "Utilization"

    @property
    def native_value(self) -> float | None:
        """Returnerar kapacitetsutnyttjande i procent, eller None om ej beräknat.

        Beräknas baserat på aktiva faser. Om max_ampere är 0 (ingen fas konfigurerad)
        returneras None för att undvika division med noll.
        """
        result = self._coordinator.last_result
        if result is None:
            return None

        # Beräkna max_ampere som summan av aktiva fasers max_ampere
        phases = self._coordinator.phases
        active_phases = result.active_phases
        if not phases or not active_phases:
            return None

        # Hitta max_ampere för aktiva faser (index = fasnummer - 1)
        active_max_values: list[float] = []
        for phase_num in active_phases:
            idx = phase_num - 1
            if 0 <= idx < len(phases):
                raw_max = phases[idx].get("max_ampere")
                if raw_max is None or raw_max <= 0:
                    _LOGGER.warning(
                        "Fas %s saknar eller har ogiltigt max_ampere (%r) — "
                        "hoppar över i utilization-beräkning",
                        phase_num,
                        raw_max,
                    )
                    continue
                active_max_values.append(float(raw_max))
            else:
                _LOGGER.warning(
                    "Fasnummer %s från aktiva faser utanför konfigurationsintervallet "
                    "(konfigurerade faser: %s) — hoppar över",
                    phase_num,
                    len(phases),
                )

        if not active_max_values:
            return None

        # Använd minsta max_ampere bland aktiva faser (konservativt)
        max_ampere = min(active_max_values)
        if max_ampere <= 0:
            return None

        # Beräkna utnyttjandegrad och kläm till [0, 100]
        utilization = (max_ampere - result.available_min) / max_ampere * 100
        return round(max(0.0, min(100.0, utilization)), 1)
