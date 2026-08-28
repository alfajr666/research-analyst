"""Rotation feed (phase 9, specs/binance-oi-rotation-*).

Exports the currently-active Binance OI rotation members into the JSON feed that
`ws_gateway.load_rotated_bases()` consumes when `WS_SYMBOL_SOURCE=rotated|both`.
Disabled by default (`ROTATION_FEED_ENABLED=false`); also requires the symbol
source to actually ask for rotated symbols.

This is the only writer of `BINANCE_OI_ROTATION_FEED_PATH`. It reads the rotation
membership from the dedicated Binance OI DB and never touches credentials, orders,
or the market DB write path.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import config


def _active_rotation_assets() -> List[str]:
    """Distinct assets currently entered/active in the OI rotation watchlist."""
    try:
        config.init_binance_oi_db()
        conn = config.get_db_connection(read_only=True, db_path=getattr(config, "BINANCE_OI_DB_PATH", None))
    except Exception:
        return []
    try:
        now = datetime.now(timezone.utc)
        rows = conn.execute(
            """
            SELECT DISTINCT asset FROM binance_oi_rotation_watchlist_history
            WHERE state IN ('entered', 'active') AND expires_at > ?
            ORDER BY asset
            """,
            (now,),
        ).fetchall()
        return [r[0] for r in rows if r and r[0]]
    except Exception:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


def refresh_rotation_feed(feed_path: Optional[Path] = None) -> Dict[str, Any]:
    """Write the rotation feed JSON. No-op when disabled (per phase 9 default)."""
    if not getattr(config, "ROTATION_FEED_ENABLED", False):
        return {"enabled": False, "written": 0}
    feed_path = Path(feed_path or getattr(config, "BINANCE_OI_ROTATION_FEED_PATH", ""))
    assets = _active_rotation_assets()
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "binance_oi_rotation_watchlist",
        "candidates": assets,
    }
    try:
        feed_path.parent.mkdir(parents=True, exist_ok=True)
        feed_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as e:
        return {"enabled": True, "written": 0, "error": str(e)[:200]}
    return {"enabled": True, "written": len(assets), "path": str(feed_path)}


if __name__ == "__main__":
    print(json.dumps(refresh_rotation_feed(), default=str))
