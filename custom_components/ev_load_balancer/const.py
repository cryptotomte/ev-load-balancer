"""Konstanter för EV Load Balancer."""

DOMAIN = "ev_load_balancer"

# Säkerhetsmarginal i ampere (subtraheras från tillgänglig kapacitet)
DEFAULT_SAFETY_MARGIN = 2

# Lägsta tillåtna laddström i ampere (IEC 61851-standard)
DEFAULT_MIN_CURRENT = 6

# Högsta tillåtna laddström i ampere
DEFAULT_MAX_CURRENT = 16

# Standard fasantal ("auto" = automatisk detektering)
DEFAULT_PHASE_COUNT = "auto"

# --- Config entry-nycklar ---

CONF_PROFILE_ID = "profile_id"
"""Nyckel för vald laddarprofil-ID."""

CONF_SERIAL = "serial"
"""Nyckel för laddarens serienummer."""

CONF_CHARGER_ENTITIES = "charger_entities"
"""Nyckel för dict med lösade entitets-ID:n."""

CONF_PHASES = "phases"
"""Nyckel för lista med faskonfigurationer."""

CONF_SAFETY_MARGIN = "safety_margin"
"""Nyckel för säkerhetsmarginal i ampere."""

CONF_MIN_CURRENT = "min_current"
"""Nyckel för lägsta tillåtna laddström."""

CONF_MAX_CURRENT = "max_current"
"""Nyckel för högsta tillåtna laddström."""

CONF_PHASE_COUNT = "phase_count"
"""Nyckel för fasantalsinställning ('auto', '1' eller '3')."""

# --- Cooldown och sensor-namnkonstanter (PR-03) ---

COOLDOWN_SECONDS = 5.0
"""Cooldown i sekunder för uppåtreglering (Debouncer trailing edge)."""

# --- Hysteres-konstanter (PR-04) ---

PAUSE_DELAY_SECONDS = 15.0
"""Antal sekunder under min_current innan PAUSE skickas."""

RESUME_DELAY_SECONDS = 30.0
"""Antal sekunder över resume_threshold innan RESUME skickas."""

RESUME_THRESHOLD_OFFSET = 2
"""Offset i ampere ovanför min_current för att trigga resume (hysteres)."""

# Suffix för sensor-entitets-ID:n
SENSOR_STATUS = "status"
"""Status-sensorns entitets-ID suffix."""

SENSOR_AVAILABLE_L1 = "available_l1"
"""Tillgänglig ström L1 — sensorns ID suffix."""

SENSOR_AVAILABLE_L2 = "available_l2"
"""Tillgänglig ström L2 — sensorns ID suffix."""

SENSOR_AVAILABLE_L3 = "available_l3"
"""Tillgänglig ström L3 — sensorns ID suffix."""

SENSOR_AVAILABLE_MIN = "available_min"
"""Minsta tillgängliga ström (min av aktiva faser) — sensorns ID suffix."""

SENSOR_TARGET_CURRENT = "target_current"
"""Beräknad målström — sensorns ID suffix."""
