"""Security-domain parsers package.

Parsers are pure functions that take raw text and return structured metadata.
Each parser targets a single source type (Sigma, NVD, MITRE ATT&CK).
"""

from grimoire.strategies.security.parsers.mitre import parse_mitre
from grimoire.strategies.security.parsers.nvd import parse_nvd_json
from grimoire.strategies.security.parsers.sigma import (
    parse_sigma,
    sigma_level_to_severity,
)

__all__ = [
    "parse_mitre",
    "parse_nvd_json",
    "parse_sigma",
    "sigma_level_to_severity",
]
