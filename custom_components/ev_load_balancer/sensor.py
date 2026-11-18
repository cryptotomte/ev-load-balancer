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

import asyncio
import json
import logging
import math
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfElectricCurrent
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import start as ha_start
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_state_report_event,
    async_track_time_interval,
)
from homeassistant.util.dt import utcnow

from .calculator import CalculationResult, calculate
from .charger_profiles import PROFILES
from .command_dispatcher import CommandDispatcher
from .const import (
    CLOCK_RECALC_SECONDS,
    CONF_ACTION_ON_SENSOR_LOSS,
    CONF_CAPACITY_WARNING_THRESHOLD,
    CONF_CHARGER_ENTITIES,
    CONF_ESTIMATE_WINDOW_SECONDS,
    CONF_MAX_CURRENT,
    CONF_MIN_CURRENT,
    CONF_PHASES,
    CONF_PROFILE_ID,
    CONF_SAFE_DEFAULT_CURRENT,
    CONF_SAFETY_MARGIN,
    CONF_UP_HOLD_SECONDS,
    COOLDOWN_SECONDS,
    DEFAULT_CAPACITY_WARNING_THRESHOLD,
    DEFAULT_ESTIMATE_WINDOW_SECONDS,
    DEFAULT_MAX_CURRENT,
    DEFAULT_MIN_CURRENT,
    DEFAULT_SAFE_CURRENT,
    DEFAULT_SAFETY_MARGIN,
    DEFAULT_SENSOR_LOSS_ACTION,
    DEFAULT_UP_HOLD_SECONDS,
    DOMAIN,
    ESTIMATE_WINDOW_RANGE,
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
    SAMPLE_COALESCE_SECONDS,
    SENSOR_AVAILABLE_L1,
    SENSOR_AVAILABLE_L2,
    SENSOR_AVAILABLE_L3,
    SENSOR_AVAILABLE_MIN,
    SENSOR_STATUS,
    SENSOR_TARGET_CURRENT,
    SENSOR_UTILIZATION,
    UP_HOLD_RANGE,
)
from .hysteresis import HysteresisAction, HysteresisCommand, HysteresisController
from .load_estimator import LoadEstimator
from .phase_switcher import PhaseMode, PhaseSwitcher
from .state_machine import BalancerState, LoadBalancerStateMachine

_LOGGER = logging.getLogger(__name__)

# Bilstatus som indikerar att bil INTE är ansluten
_CAR_IDLE_STATE = "Idle"

# Laddarström per fas (A) under vilken laddaren räknas som stillastående (D-19)
_CHARGER_IDLE_MAX_A = 0.5

# Längsta väntan på en pågående beräkning vid avstängning (V-4)
_SHUTDOWN_WAIT_SECONDS = 10.0

# Fasläge i eventet phase_switched, samma form som charging_mode
_MODE_LABELS = {PhaseMode.ONE_PHASE: "1-phase", PhaseMode.THREE_PHASE: "3-phase"}


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
    - Beräkning per mätvärde från fassensorerna (spec 010 K-3, K-6)
    - Skattning av hushållslast med LoadEstimator (spec 010 K-1)
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

        # Skattning av hushållslast (spec 010), options > data > standard
        estimate_window = self._read_seconds(
            CONF_ESTIMATE_WINDOW_SECONDS, DEFAULT_ESTIMATE_WINDOW_SECONDS, ESTIMATE_WINDOW_RANGE
        )
        up_hold = self._read_seconds(CONF_UP_HOLD_SECONDS, DEFAULT_UP_HOLD_SECONDS, UP_HOLD_RANGE)
        self._estimator = LoadEstimator(len(self._phases), estimate_window, up_hold)
        # Fassensorns last_reported vid senast behandlade rapport, per fas
        self._processed_reports: list[datetime | None] = [None] * len(self._phases)
        self._phase_sensor_ids = {
            phase.get("sensor", "") for phase in self._phases if phase.get("sensor", "")
        }
        self._device_sensor_ids = {
            self._charger_entities[key]
            for key in ("nrg_4", "nrg_5", "nrg_6")
            if self._charger_entities.get(key)
        }

        # State machine och senaste beräkningsresultat
        self._state_machine = LoadBalancerStateMachine()
        self.last_result: CalculationResult | None = None
        self._pause_reason: str | None = None

        # Senast bekräftad laddström (används av hysteres-kontrollern)
        self._last_sent_amp: int = self._min_current
        # D-19: efter inkoppling, start eller omladdning är laddarens inställda ström
        # okänd tills ett första kommando gått fram.
        self._first_command_pending = False

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

        # Egen sammanslagning (K-6, D-11): HA:s Debouncer tappade ett anrop som kom
        # medan beräkningen väntade på laddaren (R-2). Cooldown för höjning ligger
        # i hysteres-kontrollern.
        self._calc_timer: CALLBACK_TYPE | None = None
        self._calc_task: asyncio.Task | None = None
        self._calc_running = False
        self._calc_again = False
        self._shut_down = False

        # Kapacitetsvarnings-state (för transition-detektion)
        self._last_capacity_warning: bool = False

        # Sensorer som redan fått en varning om att de är otillgängliga
        self._unavailable_logged: set[str] = set()

        # Cleanup-callbacks (registrerade lyssnare)
        self._remove_listeners: list[Callable] = []

    def _read_seconds(self, key: str, default: int, allowed: tuple[int, int]) -> float:
        """Läs en avancerad inställning (K-9), options > data > standard.

        Ett handredigerat värde får inte stoppa integrationen (R-11): fel typ ger
        standardvärdet och värden utanför intervallet begränsas, med en varning.
        """
        raw = self.entry.options.get(key, self.entry.data.get(key, default))
        if isinstance(raw, bool) or not isinstance(raw, int | float) or not math.isfinite(raw):
            _LOGGER.warning("Ogiltigt värde för %s: %r — använder standard %s s", key, raw, default)
            return float(default)
        value = min(max(float(raw), allowed[0]), allowed[1])
        if value != raw:
            _LOGGER.warning(
                "%s=%r ligger utanför %s–%s s — använder %s s",
                key,
                raw,
                allowed[0],
                allowed[1],
                value,
            )
        return value

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
        self._last_sent_amp = self._read_charger_amp()

        # Skjut upp PSM auto + initial fasläge till efter att HA är fullt uppstartat.
        # select.select_option-tjänsten är ej tillgänglig under setup (timing-issue).
        async def _init_after_ha_start(_hass: HomeAssistant) -> None:
            if not self._car_known_idle():
                # D-13 (R-4): psm mitt i ett pass avbryter laddningen och nollställde
                # uppehållstiden. Okänd bilstatus räknas som laddande (A-10).
                psm_mode = self._read_psm_mode()
                if psm_mode is None:
                    self._phase_switcher.set_initial_mode(self._read_active_phases_sync())
                    psm_mode = self._phase_switcher.current_mode
                self._phase_switcher.record_mode_change(psm_mode, utcnow())
                _LOGGER.info(
                    "Bil ansluten vid start: psm rörs inte, fasläge %s, "
                    "ingen fasväxling förrän uppehållstiden gått",
                    psm_mode,
                )
                return
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

        self._remove_listeners.append(ha_start.async_at_started(self.hass, _init_after_ha_start))

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

        # Ett oförändrat värde som skrivs igen (state_reported) är ett nytt mätvärde.
        # P-3 är mätt falskt i referensinstallationen (spec 010 avsnitt 10): där
        # skrivs bara ändrade värden, upp till 570 s isär. Regleringen beror därför
        # inte på dessa rapporter; klockan (D-12) låter tidsvillkoren löpa ut.
        if self._phase_sensor_ids:
            self._remove_listeners.append(
                async_track_state_report_event(
                    self.hass,
                    list(self._phase_sensor_ids),
                    self._handle_phase_report,
                )
            )

        self._remove_listeners.append(
            async_track_time_interval(
                self.hass, self._on_clock, timedelta(seconds=CLOCK_RECALC_SECONDS)
            )
        )

        self._schedule_calculation()

    async def async_shutdown(self) -> None:
        """Avregistrera lyssnare och timrar; låt en pågående beräkning bli klar.

        Beräkningen avbryts inte mitt i: ett avbrutet resume kunde lämna frc='0'
        utan strömkommando. Väntan är begränsad (V-4) så att en beräkning som
        hänger inte hindrar att plattformarna laddas ur (Princip VI).
        """
        self._shut_down = True
        self._cancel_scheduled_calculation()
        for remove_fn in self._remove_listeners:
            remove_fn()
        self._remove_listeners.clear()
        if self._calc_task is not None and not self._calc_task.done():
            _, pending = await asyncio.wait({self._calc_task}, timeout=_SHUTDOWN_WAIT_SECONDS)
            if pending:
                _LOGGER.warning(
                    "Pågående beräkning inte klar efter %s s — avslutar utan att vänta",
                    _SHUTDOWN_WAIT_SECONDS,
                )
        self._notify_listeners.clear()

    @callback
    def _schedule_calculation(self) -> None:
        """Beräkna efter SAMPLE_COALESCE_SECONDS; rapporter inom den tiden blir ett mätvärde."""
        if self._shut_down or self._calc_timer is not None:
            return
        self._calc_timer = async_call_later(
            self.hass, SAMPLE_COALESCE_SECONDS, self._on_coalesce_timer
        )

    @callback
    def _on_clock(self, _now: datetime) -> None:
        """Låt tidsvillkoren löpa ut även när inga mätvärden kommer (D-12, R-3).

        Paus, återstart, uppväxling, uppehållstid och hålltid prövas bara i en
        beräkning, och fassensorn skriver inte oförändrade värden (P-3 falskt).
        Omräkningen behandlar ingen rapport, så den är inget mätvärde och gör
        ingen skattning entydig. Den går även i INITIALIZING (D-21), annars
        lämnas det läget inte förrän ett fasvärde ändras.
        """
        if self._state_machine.state in (
            BalancerState.INITIALIZING,
            BalancerState.BALANCING,
            BalancerState.PAUSED,
        ):
            self._schedule_calculation()

    @callback
    def _cancel_scheduled_calculation(self) -> None:
        """Avbryt en väntande beräkning och en begärd omkörning."""
        if self._calc_timer is not None:
            self._calc_timer()
            self._calc_timer = None
        self._calc_again = False

    @callback
    def _on_coalesce_timer(self, _now: datetime) -> None:
        self._calc_timer = None
        self._start_calculation()

    @callback
    def _start_calculation(self) -> None:
        """Beräkna nu, eller direkt efter den beräkning som pågår (D-11).

        En rapport som kommer medan beräkningen väntar på laddaren tappas alltså
        inte; den väntar högst sammanslagningen plus den pågående beräkningen.
        """
        if self._shut_down:
            return
        if self._calc_running:
            self._calc_again = True
            return
        self._calc_running = True
        self._calc_task = self.hass.async_create_task(self._async_run_calculations())

    async def _async_run_calculations(self) -> None:
        try:
            while True:
                self._calc_again = False
                try:
                    await self._async_calculate()
                except Exception:
                    # V-4: en väntande omräkning får inte tappas, och avstängningen
                    # får inte avbrytas av felet.
                    _LOGGER.exception("Oväntat fel i beräkningen")
                if not self._calc_again or self._shut_down:
                    return
        finally:
            self._calc_running = False

    @callback
    def _handle_state_change(self, event: Event) -> None:
        """Hanterar state change-event för bevakade entiteter.

        - Laddarens ström (nrg_4/5/6) uppdaterar bara historiken (K-3, F-2).
        - Fassensor unavailable → failsafe; tillgänglig igen i FAILSAFE → återhämtning.
        - Fassensorer, car_value, map och pha schemalägger en beräkning.
        """
        entity_id = event.data.get("entity_id", "")
        new_state = event.data.get("new_state")

        if entity_id in self._device_sensor_ids:
            self._estimator.record_device(utcnow(), *self._read_device_sync())
            return

        if entity_id in self._phase_sensor_ids:
            if new_state is not None and new_state.state == "unavailable":
                self._cancel_scheduled_calculation()
                self.hass.async_create_task(self._async_handle_sensor_unavailable())
                return

            if new_state is not None and new_state.state not in ("unavailable", "unknown"):
                if self._state_machine.state == BalancerState.FAILSAFE:
                    self._cancel_scheduled_calculation()
                    self.hass.async_create_task(self._async_check_recovery())
                    return

        if self._state_machine.state == BalancerState.FAILSAFE:
            return

        if entity_id == self._charger_entities.get("car_value"):
            # D-10 b, D-19: inkopplad bil får sitt första kommando direkt, utan sammanslagning.
            self._start_calculation()
            return

        self._schedule_calculation()

    @callback
    def _handle_phase_report(self, event: Event) -> None:
        """Fassensor skrev samma värde igen: ett nytt mätvärde (K-1, K-3)."""
        if self._state_machine.state != BalancerState.FAILSAFE:
            self._schedule_calculation()

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
            if await self._dispatcher.pause():
                self._estimator.record_command(utcnow(), [0.0] * len(self._phases))
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

            now = utcnow()
            if self._action_on_sensor_loss == "pause":
                if await self._dispatcher.pause():
                    self._estimator.record_command(now, [0.0] * len(self._phases))
            elif await self._dispatcher.send_amp(self._safe_default_current):
                # R-7: annars blir nästa "sänkning" en okontrollerad höjning
                self._last_sent_amp = self._safe_default_current
                self._hysteresis.record_amp_change(now)
                # D-16; alla faser, eftersom aktiva faser inte är kända under failsafe
                self._estimator.record_command(
                    now, [float(self._safe_default_current)] * len(self._phases)
                )

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
        # D-19: amp läses inte om; entiteten kan ännu visa värdet från före failsafe.
        self._notify_all_listeners()

        # Starta beräkningscykel för att återta normal drift
        self._schedule_calculation()

    def _car_known_idle(self) -> bool:
        """True bara när car_value går att läsa och visar att ingen bil är ansluten."""
        car_entity = self._charger_entities.get("car_value", "")
        state = self.hass.states.get(car_entity) if car_entity else None
        return state is not None and state.state == _CAR_IDLE_STATE

    def _read_psm_mode(self) -> PhaseMode | None:
        """Fasläget enligt psm-entiteten; None vid auto eller om den inte går att läsa."""
        psm_entity = self._charger_entities.get("psm", "")
        state = self.hass.states.get(psm_entity) if psm_entity else None
        if state is None:
            return None
        return {
            PSM_VALUE_1PHASE: PhaseMode.ONE_PHASE,
            PSM_VALUE_3PHASE: PhaseMode.THREE_PHASE,
        }.get(state.state)

    def _log_unavailable(self, entity_id: str, message: str, *args: Any) -> None:
        """WARNING när sensorn blir otillgänglig, sedan DEBUG tills den är tillbaka.

        Med klockan (D-12) och varje rapport skulle samma varning annars upprepas
        var femte sekund (13.3).
        """
        level = logging.DEBUG if entity_id in self._unavailable_logged else logging.WARNING
        self._unavailable_logged.add(entity_id)
        _LOGGER.log(level, message, *args)

    def _read_charger_amp(self) -> int:
        """Laddarens inställda ström enligt amp-entiteten, max_current om den inte går att läsa.

        Bara ett utgångsvärde: första kommandot efter inkoppling (D-19) ersätter det
        innan någon höjning prövas.
        """
        entity_id = self._charger_entities.get("amp", "")
        state = self.hass.states.get(entity_id) if entity_id else None
        if state is not None:
            try:
                return math.ceil(float(state.state))
            except (ValueError, TypeError, OverflowError):
                pass
        _LOGGER.info(
            "Laddarens ström kan inte läsas från '%s' — antar %sA",
            entity_id,
            self._max_current,
        )
        return self._max_current

    def _read_device_values_sync(self) -> list[float]:
        """Läs laddarens ström per fas synkront (se _read_device_sync)."""
        return self._read_device_sync()[0]

    def _read_device_sync(self) -> tuple[list[float], list[bool]]:
        """Läs laddarens ström per fas synkront, med flagga för ersättningsvärde.

        Fallback-logik (PR-05/US3):
        - Om laddarens nrg-sensor är unavailable: använd _last_sent_amp
          som fallback-värde och logga WARNING.
        - Om sensorn saknas helt: använd 0.0A som tidigare.
        Ett ersättningsvärde flaggas; det gör aldrig ett mätvärde entydigt (D-16).
        """
        device_values: list[float] = []
        substitute: list[bool] = []
        for _, key in zip(self._phases, ("nrg_4", "nrg_5", "nrg_6")):
            entity_id = self._charger_entities.get(key, "")
            substitute.append(True)
            if entity_id:
                state = self.hass.states.get(entity_id)
                if state and state.state not in ("unavailable", "unknown", ""):
                    try:
                        device_values.append(float(state.state))
                        substitute[-1] = False
                        self._unavailable_logged.discard(entity_id)
                        continue
                    except (ValueError, TypeError):
                        _LOGGER.debug(
                            "Enhetsensor '%s' har ogiltigt värde '%s' — använder 0.0A",
                            entity_id,
                            state.state,
                        )
                elif state and state.state in ("unavailable", "unknown"):
                    # Laddarens sensor unavailable — fallback till senast satta amp-värde
                    self._log_unavailable(
                        entity_id,
                        "Laddarens sensor '%s' är %s — använder senast satt ström %sA som fallback",
                        entity_id,
                        state.state,
                        self._last_sent_amp,
                    )
                    device_values.append(float(self._last_sent_amp))
                    continue
            device_values.append(0.0)
        return device_values, substitute

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

        Ordningen följer spec 010 avsnitt 6.2:
        1. Nya fasrapporter (ändrad last_reported) uppdaterar skattningen (K-1),
           med last_reported som rapporttid (D-11).
        2. Budget per fas och mål via calculate().
        3. Fasväxlaren (med uppehållstiden i K-5).
        4. Hysteres: sänkning direkt, höjning högst till taket i K-4, paus som förut.
        5. Logg enligt K-7, sedan lyssnarna.
        FAILSAFE och otillgängliga fassensorer avbryter beräkningen som förut.
        """
        if not self._phases:
            _LOGGER.warning("Inga fassensorer konfigurerade — beräkning avbryts")
            return

        # FAILSAFE: hoppa över normal beräkning — failsafe-logik hanteras av
        # _async_handle_sensor_unavailable() och _async_check_recovery()
        if self._state_machine.state == BalancerState.FAILSAFE:
            _LOGGER.debug("Beräkning avbryts: systemet är i FAILSAFE-tillstånd")
            return

        now = utcnow()

        # Läs fassensorvärden — kontrollera unavailable
        phase_values: list[float] = []
        phase_reported: list[datetime] = []
        for phase in self._phases:
            sensor_id = phase.get("sensor", "")
            if not sensor_id:
                _LOGGER.warning("Fas saknar sensor-ID — beräkning avbryts")
                self.last_result = None
                self._notify_all_listeners()
                return
            state = self.hass.states.get(sensor_id)
            if state is None or state.state in ("unavailable", "unknown"):
                self._log_unavailable(
                    sensor_id,
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
                phase_reported.append(state.last_reported)
                self._unavailable_logged.discard(sensor_id)
            except (ValueError, TypeError):
                _LOGGER.warning(
                    "Fassensor '%s' har ogiltigt värde '%s' — beräkning avbryts",
                    sensor_id,
                    state.state,
                )
                self.last_result = None
                self._notify_all_listeners()
                return

        device_values, substitute = self._read_device_sync()
        self._estimator.record_device(now, device_values, substitute)
        new_samples: set[int] = set()
        for idx, reported in enumerate(phase_reported):
            if reported != self._processed_reports[idx]:
                self._processed_reports[idx] = reported
                self._estimator.on_phase_report(reported, idx, phase_values[idx])
                new_samples.add(idx)
        estimates = self._estimator.estimates

        # Parsa map för aktiva faser
        active_phase_numbers = self._read_active_phases_sync()

        # Kör beräkning
        try:
            result = calculate(
                phases=self._phases,
                phase_values=phase_values,
                household_loads=[estimate.household for estimate in estimates],
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

        self.last_result = result

        # Registrera lyckad beräkning (alla sensorer tillgängliga)
        prev_state = self._state_machine.state
        transitioned = self._state_machine.record_successful_calculation()
        if transitioned:
            _LOGGER.info(
                "Tillståndsändring: %s → %s (2 lyckade beräkningar)",
                prev_state,
                self._state_machine.state,
            )

        n_phases = len(self._phases)
        active_idx = [p - 1 for p in result.active_phases if 0 < p <= n_phases]

        # Hantera bilstatus → IDLE/BALANCING-övergångar
        if await self._async_handle_car_status(now, device_values, active_idx):
            self._first_command_pending = True

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
                now=now,
            )
            switch_allowed = phase_cmd is not None
            if phase_cmd is not None and phase_cmd.target_mode == PhaseMode.THREE_PHASE:
                # D-14 (R-5): efter uppväxlingen drar laddaren på alla faser, så
                # strömmen räknas på dem i samma beräkning och en nödvändig sänkning
                # går före psm. Går den inte att skicka görs ingen uppväxling.
                result = calculate(
                    phases=self._phases,
                    phase_values=phase_values,
                    household_loads=[estimate.household for estimate in estimates],
                    device_values=device_values,
                    active_phase_numbers=list(range(1, n_phases + 1)),
                    safety_margin=self._safety_margin,
                    min_current=self._min_current,
                    max_current=self._max_current,
                )
                self.last_result = result
                active_idx = list(range(n_phases))
                # D-20 (V-2): faserna som kommer till får inte mer än hålltidens tak.
                amp = min(
                    result.target_current,
                    self._hold_ceiling(now, phase_values, device_values, active_idx),
                )
                if amp < self._min_current:
                    switch_allowed = False
                elif amp < self._last_sent_amp:
                    switch_allowed = await self._async_set_amp(
                        now,
                        HysteresisCommand(
                            action=HysteresisAction.SET_AMP,
                            amp=amp,
                            reason=f"Före uppväxling 1→3: {self._last_sent_amp}A → {amp}A",
                        ),
                        result,
                        device_values,
                        active_idx,
                    )
            if switch_allowed:
                psm_value = (
                    PSM_VALUE_1PHASE
                    if phase_cmd.target_mode == PhaseMode.ONE_PHASE
                    else PSM_VALUE_3PHASE
                )
                psm_sent = await self._dispatcher.send_psm(psm_value)
                if psm_sent:
                    # D-16: laddningen avbryts och startar på nya faser.
                    self._estimator.record_command(now, [0.0] * n_phases)
                    self._estimator.record_command(now, [float(self._last_sent_amp)] * n_phases)
                    from_mode = self._phase_switcher.current_mode
                    self._phase_switcher.record_mode_change(phase_cmd.target_mode, now)
                    self.fire_phase_switched_event(
                        from_mode=_MODE_LABELS[from_mode],
                        to_mode=_MODE_LABELS[phase_cmd.target_mode],
                        reason=phase_cmd.reason,
                        available_per_phase=[
                            result.charger_budget_l1,
                            result.charger_budget_l2,
                            result.charger_budget_l3,
                        ],
                    )

        # Hantera kapacitetsbrist/reglering via hysteres (PR-04)
        current_sm_state = self._state_machine.state
        if current_sm_state in (BalancerState.BALANCING, BalancerState.PAUSED):
            is_paused = current_sm_state == BalancerState.PAUSED
            first_command = (
                self._first_command_pending and current_sm_state == BalancerState.BALANCING
            )

            if first_command:
                cmd = self._first_command(
                    now, result, phase_values, device_values, substitute, active_idx
                )
            else:
                cmd = self._hysteresis.evaluate(
                    available_min=result.available_min,
                    target_current=result.target_current,
                    last_sent_amp=self._last_sent_amp,
                    is_paused=is_paused,
                    now=now,
                    up_ceiling=self._up_ceiling(now, phase_values, device_values, active_idx),
                )

            if cmd.action == HysteresisAction.SET_AMP:
                sent = await self._async_set_amp(now, cmd, result, device_values, active_idx)
                if first_command and sent:
                    self._first_command_pending = False

            elif cmd.action == HysteresisAction.PAUSE:
                # Pausa laddning — övergå till PAUSED
                paused = await self._dispatcher.pause()
                if paused:
                    self._estimator.record_command(now, [0.0] * n_phases)
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
                    self._record_command(now, cmd.amp, device_values, active_idx)
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

        self._log_estimates(result, new_samples)

        # VIKTIGT: kapacitetsvarning kontrolleras FÖRE _notify_all_listeners() för att
        # säkerställa att event skickas på HA:s eventbuss innan binary_sensor:s
        # state skrivs till HA via async_write_ha_state.
        self._check_capacity_warning(result)

        # Notifiera alla sensorlyssnare om uppdatering
        self._notify_all_listeners()

    async def _async_set_amp(
        self,
        now: datetime,
        cmd: HysteresisCommand,
        result: CalculationResult,
        device_values: list[float],
        active_idx: list[int],
    ) -> bool:
        """Skicka cmd.amp; vid lyckad sändning uppdateras senast skickat, fönstret och eventet."""
        old_amp = self._last_sent_amp
        direction = "sänkt" if cmd.amp < old_amp else "höjd" if cmd.amp > old_amp else "satt"
        if not await self._dispatcher.send_amp(cmd.amp):
            return False
        self._last_sent_amp = cmd.amp
        self._hysteresis.record_amp_change(now)
        self._record_command(now, cmd.amp, device_values, active_idx)
        _LOGGER.info("Ström %s: %sA → %sA (%s)", direction, old_amp, cmd.amp, cmd.reason)
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
        return True

    def _first_command(
        self,
        now: datetime,
        result: CalculationResult,
        phase_values: list[float],
        device_values: list[float],
        substitute: list[bool],
        active_idx: list[int],
    ) -> HysteresisCommand:
        """Första kommandot efter att bilen kopplats in, även vid start och omladdning (D-19).

        amp-entiteten kan visa fel värde, så det skickas alltid. Drar laddaren
        ström blir det högst den uppmätta strömmen avrundad uppåt och kan aldrig
        höja det laddaren faktiskt drar. Drar den ingen ström är skattningen exakt
        (D-10 b) och värdet begränsas av hålltidens tak (A-9); samma tak gäller när
        strömmen inte går att mäta (A-17).
        """
        measured = [value for value, flag in zip(device_values, substitute) if not flag]
        drawing = any(substitute) or max(device_values, default=0.0) >= _CHARGER_IDLE_MAX_A
        if drawing and measured:
            cap = math.ceil(max(measured))
            source = f"laddaren drar {max(measured):.1f}A"
        else:
            cap = self._hold_ceiling(now, phase_values, device_values, active_idx)
            source = "laddaren drar ingen ström" if not drawing else "ström kan inte mätas"
        amp = max(self._min_current, min(result.target_current, cap))
        return HysteresisCommand(
            action=HysteresisAction.SET_AMP,
            amp=amp,
            reason=f"Första kommando efter inkoppling ({source}): {amp}A",
        )

    def _up_ceiling(
        self,
        now: datetime,
        phase_values: list[float],
        device_values: list[float],
        active_idx: list[int],
    ) -> int | None:
        """Högsta ström en höjning eller återstart får gå till (spec 010 K-4, D-14).

        None om senaste mätvärdet på någon aktiv fas inte var entydigt (F-3);
        annars målet med den högsta skattningen under hålltiden.
        """
        estimates = self._estimator.estimates
        # D-16: utan aktiv fas i konfigurationen bevisar inget mätvärde något.
        if not active_idx or not all(estimates[idx].tight for idx in active_idx):
            return None
        return self._hold_ceiling(now, phase_values, device_values, active_idx)

    def _hold_ceiling(
        self,
        now: datetime,
        phase_values: list[float],
        device_values: list[float],
        active_idx: list[int],
    ) -> int:
        """Målet med den högsta skattningen per fas under hålltiden (K-4).

        Inte uppklämt till min_current: under min ska ingen återstart ske (D-14).
        """
        peak = calculate(
            phases=self._phases,
            phase_values=phase_values,
            household_loads=self._estimator.peak(now),
            device_values=device_values,
            active_phase_numbers=[idx + 1 for idx in active_idx],
            safety_margin=self._safety_margin,
            min_current=self._min_current,
            max_current=self._max_current,
        )
        return min(self._max_current, math.floor(peak.available_min))

    def _record_command(
        self, now: datetime, amp: int, device_values: list[float], active_idx: list[int]
    ) -> None:
        """Räkna ett skickat strömkommando som en ändring i fönstret (spec 010 6.1, P-2).

        Gäller de aktiva faserna; övriga faser behåller sitt aktuella värde.
        """
        self._estimator.record_command(
            now,
            [float(amp) if idx in active_idx else value for idx, value in enumerate(device_values)],
        )

    def _log_estimates(self, result: CalculationResult, new_samples: set[int]) -> None:
        """Logga skattningen per fas på DEBUG (spec 010 K-7).

        nytt=ja: fasen fick ett nytt mätvärde i den här beräkningen; nej: värdet
        är från en tidigare rapport (klockan, bilstatus, fasläge).
        """
        budgets = [result.charger_budget_l1, result.charger_budget_l2, result.charger_budget_l3]
        for idx, estimate in enumerate(self._estimator.estimates):
            _LOGGER.debug(
                "Skattning %s: nytt=%s P=%.2fA laddare_min=%.2fA laddare_max=%.2fA "
                "låg=%.2fA hög=%.2fA h=%.2fA entydig=%s budget=%.2fA",
                self._phases[idx].get("label", f"L{idx + 1}"),
                "ja" if idx in new_samples else "nej",
                estimate.reported,
                estimate.device_min,
                estimate.device_max,
                estimate.low,
                estimate.high,
                estimate.household,
                estimate.tight,
                budgets[idx],
            )

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

    async def _async_handle_car_status(
        self,
        now: datetime,
        device_values: list[float],
        active_idx: list[int],
    ) -> bool:
        """Hanterar bilstatus för IDLE/BALANCING-övergångar.

        Läser car_value-sensorn och triggar övergångar i state machine.
        Not: FAILSAFE-tillstånd hanteras inte här (det är en fassensor-händelse).

        Returns:
            True vid IDLE → BALANCING, även vid start med laddande bil (D-19).
        """
        car_entity = self._charger_entities.get("car_value", "")
        if not car_entity:
            return False

        state = self.hass.states.get(car_entity)
        if state is None or state.state in ("unavailable", "unknown"):
            _LOGGER.debug(
                "Bilsensor '%s' är %s — hoppar över statushantering",
                car_entity,
                state.state if state else "saknas",
            )
            return False

        car_state = state.state
        sm_state = self._state_machine.state

        if sm_state == BalancerState.IDLE and car_state != _CAR_IDLE_STATE:
            # Bil ansluten — övergå till BALANCING
            if self._state_machine.on_car_connected():
                _LOGGER.info("Tillståndsändring: IDLE → BALANCING (car_value='%s')", car_state)
                return True
        elif (
            sm_state in (BalancerState.BALANCING, BalancerState.PAUSED)
            and car_state == _CAR_IDLE_STATE
        ):
            # Bil bortkopplad — återgå till IDLE
            prev = sm_state
            if self._state_machine.on_car_disconnected():
                self._pause_reason = None
                self._hysteresis.reset()
                _LOGGER.info("Tillståndsändring: %s → IDLE (car_value='Idle')", prev)
                # D-10: laddaren ska stå där koden tror inför nästa inkoppling.
                if await self._dispatcher.send_amp(self._min_current):
                    self._last_sent_amp = self._min_current
                    self._record_command(now, self._min_current, device_values, active_idx)
                else:
                    self._last_sent_amp = self._read_charger_amp()
        return False


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
