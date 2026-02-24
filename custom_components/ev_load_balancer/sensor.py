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
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util.dt import utcnow

from .calculator import CalculationResult, calculate
from .command_dispatcher import CommandDispatcher
from .const import (
    CONF_CAPACITY_WARNING_THRESHOLD,
    CONF_CHARGER_ENTITIES,
    CONF_MAX_CURRENT,
    CONF_MIN_CURRENT,
    CONF_PHASES,
    CONF_PROFILE_ID,
    CONF_SAFETY_MARGIN,
    COOLDOWN_SECONDS,
    DEFAULT_CAPACITY_WARNING_THRESHOLD,
    DEFAULT_MAX_CURRENT,
    DEFAULT_MIN_CURRENT,
    DEFAULT_SAFETY_MARGIN,
    DOMAIN,
    EVENT_CAPACITY_WARNING,
    EVENT_CURRENT_ADJUSTED,
    EVENT_DEVICE_PAUSED,
    EVENT_DEVICE_RESUMED,
    EVENT_FAILSAFE_ACTIVATED,
    EVENT_PHASE_SWITCHED,
    EVENT_SENSOR_LOST,
    PAUSE_DELAY_SECONDS,
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

        Lägger automatiskt till entity_id och ISO 8601-tidsstämpel i event-data.
        Hanterar eventuella fel via try/except för att inte påverka styrlogiken.

        Args:
            event_type: Event-typ, t.ex. EVENT_CURRENT_ADJUSTED.
            data: Extra kontext-data att inkludera i eventet.
        """
        event_data = {
            "entity_id": f"sensor.{DOMAIN}_{SENSOR_STATUS}",
            "timestamp": utcnow().isoformat(),
            **data,
        }
        try:
            self.hass.bus.async_fire(event_type, event_data)
        except Exception:
            _LOGGER.error(
                "Misslyckades att skicka event '%s'",
                event_type,
                exc_info=True,
            )

    def _get_capacity_warning_threshold(self) -> int:
        """Läs kapacitetsvarnings-tröskel med options > data > default-prioritering."""
        opts = self.entry.options
        data = self.entry.data
        return int(
            opts.get(
                CONF_CAPACITY_WARNING_THRESHOLD,
                data.get(CONF_CAPACITY_WARNING_THRESHOLD, DEFAULT_CAPACITY_WARNING_THRESHOLD),
            )
        )

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

        self._last_capacity_warning = current_warning

    def fire_sensor_lost_event(self, sensor_entity: str, action_taken: str) -> None:
        """Skicka event för sensorförlust.

        Anropas externt (t.ex. vid failsafe-hantering) när en sensor
        blir unavailable och en åtgärd vidtagits.

        Args:
            sensor_entity: Entitets-ID för sensorn som försvann.
            action_taken: Beskrivning av vidtagen åtgärd.
        """
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
        # Samla alla entitets-ID:n att bevaka
        entities_to_track: list[str] = []

        for phase in self._phases:
            sensor_id = phase.get("sensor", "")
            if sensor_id:
                entities_to_track.append(sensor_id)

        # Laddar-entiteter att bevaka
        for key in ("car_value", "map", "nrg_4", "nrg_5", "nrg_6"):
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
        - Beräkna preview (snabb synkron förhandsberäkning)
        - Om preview < current_target: nedreglering → avbryt debouncer + direkt task
        - Annars: uppåtreglering → schemalägg via debouncer (cooldown 5s)
        """
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
        """Läs laddarens ström per fas synkront (0.0 om saknas)."""
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
            device_values.append(0.0)
        return device_values

    def _read_active_phases_sync(self) -> list[int]:
        """Läs aktiva fasnummer från map-sensor synkront.

        Returnerar lista med fasnummer, eller fallback till alla konfigurerade faser
        om map-sensorn är unavailable.
        """
        map_entity = self._charger_entities.get("map", "")
        if map_entity:
            state = self.hass.states.get(map_entity)
            if state and state.state not in ("unavailable", "unknown", ""):
                try:
                    parsed = json.loads(state.state)
                    if isinstance(parsed, list):
                        return [int(x) for x in parsed]
                except (json.JSONDecodeError, ValueError, TypeError):
                    _LOGGER.warning(
                        "Map-sensor '%s' har ogiltigt värde '%s' — faller tillbaka till alla faser",
                        map_entity,
                        state.state,
                    )

        # Fallback: alla konfigurerade faser
        return list(range(1, len(self._phases) + 1))

    async def _async_calculate(self) -> None:
        """Asynkron beräkning — läser HA-state, kör calculator, skickar kommandon.

        Flöde:
        1. Läser fassensorvärden — avbryter om unavailable/unknown (FR-002/C1).
        2. Läser enhetsvärden (nrg_4/5/6) och aktiva faser (map).
        3. Kör calculate() för att beräkna available_* och target_current.
        4. Registrerar lyckad beräkning via state machine (INITIALIZING → IDLE efter 2 st).
        5. Hanterar bilstatus för IDLE/BALANCING-övergångar (car_value).
        6. I BALANCING eller PAUSED: utvärderar hysteres-kontrollern (PR-04) och
           skickar kommandon via CommandDispatcher. Uppdaterar _last_sent_amp och
           anropar record_amp_change() endast om dispatchen lyckades (bool True).
        7. Notifierar alla registrerade sensorlyssnare.
        """
        if not self._phases:
            _LOGGER.warning("Inga fassensorer konfigurerade — beräkning avbryts")
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
                                result.available_l1,
                                result.available_l2,
                                result.available_l3,
                            ],
                        },
                    )

            else:
                # NONE — ingen åtgärd
                _LOGGER.debug(
                    "Hysteres: ingen åtgärd (%s)",
                    cmd.reason,
                )

        # Kontrollera kapacitetsvarning och skicka event vid övergång
        self._check_capacity_warning(result)

        # Notifiera alla sensorlyssnare om uppdatering
        self._notify_all_listeners()

    def _handle_car_status(self) -> None:
        """Hanterar bilstatus för IDLE/BALANCING-övergångar.

        Läser car_value-sensorn och triggar övergångar i state machine.
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
        """Returnerar alla 10 status-attribut (FR-019)."""
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
    _PHASE_ATTR: dict[str, str] = {
        "l1": "available_l1",
        "l2": "available_l2",
        "l3": "available_l3",
        "min": "available_min",
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
                active_max_values.append(float(phases[idx].get("max_ampere", 0)))

        if not active_max_values:
            return None

        # Använd minsta max_ampere bland aktiva faser (konservativt)
        max_ampere = min(active_max_values)
        if max_ampere <= 0:
            return None

        # Beräkna utnyttjandegrad och kläm till [0, 100]
        utilization = (max_ampere - result.available_min) / max_ampere * 100
        return round(max(0.0, min(100.0, utilization)), 1)
