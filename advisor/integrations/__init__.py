"""Tenant data read from files the user exports themselves (no credentials, no network).

- `power_automate`: existing flows, so the design reuses rather than duplicates them.
- `licenses`: licence inventory, so spare licences offset the incremental cost.
"""

from advisor.integrations.licenses import LicenseInventory, LicenseRow, load_license_csv
from advisor.integrations.power_automate import FlowInfo, Overlap, find_overlaps, load_flows

__all__ = [
    "FlowInfo",
    "LicenseInventory",
    "LicenseRow",
    "Overlap",
    "find_overlaps",
    "load_flows",
    "load_license_csv",
]
