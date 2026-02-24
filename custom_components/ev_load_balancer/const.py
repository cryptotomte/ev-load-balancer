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
