"""Durable raw-candidate ledger and non-blocking Discord batch publisher."""
from __future__ import annotations
import hashlib, json
from datetime import datetime, timezone, timedelta
from uuid import uuid4
import config
from discord_transport import DiscordWebhookTransport

def _utc(value):
    d = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)

def window_start(value):
    d = _utc(value).replace(second=0, microsecond=0)
    return d.replace(minute=d.minute - d.minute % config.RAW_SIGNAL_DISCORD_BATCH_MINUTES)

def capture(event, db_path=None):
    """Best effort only: this function cannot affect alpha or intent delivery."""
    try:
        observed = _utc(event["observed_at"])
        material = "|".join(str(event.get(k, "")) for k in ("strategy_id", "asset", "direction", "observed_at"))
        raw_id = hashlib.sha256(material.encode()).hexdigest()
        payload = json.dumps(event, sort_keys=True, separators=(",", ":"), default=str)
        conn = config.get_db_connection(db_path=db_path or config.ANALYST_DB_PATH)
        conn.execute("INSERT OR IGNORE INTO raw_signals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (raw_id, event.get("candidate_id", raw_id), event["strategy_id"], event["asset"],
                      event["direction"], observed.isoformat().replace("+00:00", "Z"),
                      str(event.get("valid_until", observed)), payload, datetime.now(timezone.utc).isoformat()))
        conn.execute("""INSERT OR IGNORE INTO raw_signal_status_history
                      VALUES (?, ?, 'pending', 'pending', 'pending', 'not_eligible', NULL, ?)""",
                     (f"{raw_id}:captured", raw_id, datetime.now(timezone.utc).isoformat()))
        conn.commit(); conn.close()
        return raw_id
    except Exception as exc:
        print(f"raw signal capture error: {exc}")
        return None

def record_status(raw_signal_id, *, hard_gate_status=None, score_status=None,
                  clash_status=None, executor_intent_status=None, reason=None, db_path=None):
    """Append a downstream status without rewriting the raw candidate."""
    conn = config.get_db_connection(db_path=db_path or config.ANALYST_DB_PATH)
    try:
        prior = conn.execute("SELECT hard_gate_status,score_status,clash_status,executor_intent_status FROM raw_signal_status_history WHERE raw_signal_id=? ORDER BY recorded_at DESC LIMIT 1", (raw_signal_id,)).fetchone()
        values = [hard_gate_status, score_status, clash_status, executor_intent_status]
        values = [value if value is not None else (prior[i] if prior else None) for i, value in enumerate(values)]
        conn.execute("INSERT INTO raw_signal_status_history VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (f"{raw_signal_id}:{uuid4()}", raw_signal_id, *values, reason, datetime.now(timezone.utc).isoformat()))
        conn.commit()
    finally:
        conn.close()

def render(rows, start):
    end = start + timedelta(minutes=config.RAW_SIGNAL_DISCORD_BATCH_MINUTES)
    lines = ["RAW STRATEGY SIGNALS", f"Window: {start:%Y-%m-%d %H:%M}-{end:%H:%M} UTC",
             "Status: observation only; not execution-authorized"]
    for row in rows:
        p = json.loads(row[6]); entry = p.get("entry_condition", {})
        lines.append(f"{row[3]} {row[4].upper()} {row[2]} entry={entry.get('price', entry.get('type', '?'))} stop={p.get('invalidation_price', '?')} target={(p.get('targets') or ['?'])[0]}")
    lines.append(f"Totals: {len(rows)} raw")
    return "\n".join(lines)

def publish_once(now=None, db_path=None, transport=None):
    if not getattr(config, "RAW_SIGNAL_DISCORD_BATCH_ENABLED", False): return False
    now = _utc(now or datetime.now(timezone.utc)); end = window_start(now)
    start = end - timedelta(minutes=config.RAW_SIGNAL_DISCORD_BATCH_MINUTES)
    conn = config.get_db_connection(db_path=db_path or config.ANALYST_DB_PATH)
    try:
        rows = conn.execute("SELECT raw_signal_id,candidate_id,strategy_id,asset,direction,observed_at,payload_json FROM raw_signals WHERE observed_at >= ? AND observed_at < ? ORDER BY observed_at,raw_signal_id", (start.isoformat(), end.isoformat())).fetchall()
        if not rows: return False
        key = start.isoformat().replace("+00:00", "Z"); now_s = now.isoformat()
        conn.execute("INSERT OR IGNORE INTO discord_signal_batches(window_start,window_end,status,candidate_count,message_count) VALUES (?, ?, 'pending', ?, 0)", (key, end.isoformat(), len(rows)))
        conn.commit()
        claimed = conn.execute("UPDATE discord_signal_batches SET status='claimed',claimed_at=?,attempts=attempts+1 WHERE window_start=? AND (status='pending' OR (status='claimed' AND claimed_at < ?))", (now_s,key,(now-timedelta(seconds=config.RAW_BATCH_CLAIM_LEASE_SECONDS)).isoformat())).rowcount
        conn.commit()
        if not claimed: return False
    finally: conn.close()
    try:
        response = (transport or DiscordWebhookTransport(config.RAW_SIGNAL_DISCORD_WEBHOOK_URL)).send(render(rows, start))
        conn = config.get_db_connection(db_path=db_path or config.ANALYST_DB_PATH); conn.execute("UPDATE discord_signal_batches SET status='sent',sent_at=?,response_body=?,message_count=1 WHERE window_start=?", (datetime.now(timezone.utc).isoformat(), response, key)); conn.commit(); conn.close(); return True
    except Exception as exc:
        conn = config.get_db_connection(db_path=db_path or config.ANALYST_DB_PATH); conn.execute("UPDATE discord_signal_batches SET status='pending',error_message=? WHERE window_start=? AND attempts < ?", (str(exc)[:500], key, config.RAW_BATCH_MAX_ATTEMPTS)); conn.execute("UPDATE discord_signal_batches SET status='failed',error_message=? WHERE window_start=? AND attempts >= ?", (str(exc)[:500], key, config.RAW_BATCH_MAX_ATTEMPTS)); conn.commit(); conn.close(); return False
