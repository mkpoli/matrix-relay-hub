"""Static inventory validation for independently operated Matrix bridges.

Validation is not a security boundary or a connectivity check. Configuration
references are opaque strings and are never dereferenced.
"""

from .topology import ValidationError, load_topology, validate_topology

__all__ = ["ValidationError", "load_topology", "validate_topology"]
