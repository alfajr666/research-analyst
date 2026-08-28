/**
 * research-analyst PM2 apps — trading brain retired (2026-08-19).
 *
 * NT (nautilus-trading-os) owns strategies + notify + intent bus.
 * Binance OI rotation is a SEPARATE PM2 app (binance-oi-rotation-scanner);
 * do not stop it when managing this file.
 *
 * orchestrator + signal-publisher removed from apps[] so startOrReload
 * does not resurrect them.
 */
module.exports = {
  apps: [
    // retired: orchestrator (superseded by nautilus trading-node)
    // retired: signal-publisher (superseded by NT notify + intent bus)
  ],
};
