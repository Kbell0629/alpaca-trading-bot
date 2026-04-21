"""
Round-35: standalone helper for annotating positions with sector +
underlying. Extracted from server.py so tests can import it without
triggering server's full auth / sqlite init.
"""
from __future__ import annotations

import re


def annotate_sector(positions):
    """Annotate each position with `_sector` + `_underlying`.

    For options we resolve the OCC symbol to its underlying and look
    up that. Unknown symbols get "Other" (surfaces the gap to the
    operator instead of pretending we know).

    Mutates + returns the input list (for chaining with other
    annotate_* helpers). Non-list input returns unchanged.
    """
    if not isinstance(positions, list) or not positions:
        return positions
    try:
        from constants import SECTOR_MAP
    except ImportError:
        SECTOR_MAP = {}
    for p in positions:
        try:
            sym = (p.get("symbol") or "").upper()
            asset_class = (p.get("asset_class") or "").lower()
            if asset_class == "us_option":
                m = re.match(r"^([A-Z]{1,6})\d{6}[CP]\d{8}$", sym)
                underlying = m.group(1) if m else sym
            else:
                underlying = sym
            p["_underlying"] = underlying
            p["_sector"] = SECTOR_MAP.get(underlying, "Other")
        except Exception:
            p["_underlying"] = p.get("symbol", "")
            p["_sector"] = "Other"
    return positions
