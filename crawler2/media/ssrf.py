"""P8 import path of the SSRF guard; the implementation lives in ``core.netguard`` (P14)."""

from crawler2.core.netguard import (
    GuardedTransport,
    IPAddress,
    Network,
    Resolver,
    UnsafeDestinationError,
    default_resolver,
    is_unsafe_address,
    parse_networks,
    resolve_addresses,
    validate_destination,
)

__all__ = [
    "GuardedTransport",
    "IPAddress",
    "Network",
    "Resolver",
    "UnsafeDestinationError",
    "default_resolver",
    "is_unsafe_address",
    "parse_networks",
    "resolve_addresses",
    "validate_destination",
]
