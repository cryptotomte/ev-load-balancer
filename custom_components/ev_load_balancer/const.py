"""Konstanter för EV Load Balancer."""

DOMAIN = "ev_load_balancer"

# Säkerhetsmarginal i ampere (subtraheras från tillgänglig kapacitet)
DEFAULT_SAFETY_MARGIN = 2

# Lägsta tillåtna laddström i ampere (IEC 61851-standard)
DEFAULT_MIN_CURRENT = 6

# Högsta tillåtna laddström i ampere
DEFAULT_MAX_CURRENT = 16
