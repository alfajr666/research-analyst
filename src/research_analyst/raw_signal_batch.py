"""Durable raw-candidate ledger and non-blocking Discord batch publisher."""
from __future__ import annotations
import hashlib, json
from datetime import datetime, timezone, timedelta
from uuid import uuid4
import config
from discord_transport import DiscordWebhookTransport
from entry_policy import annotate_candidate, persist_observation

def _utc(value):
    d = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)

def window_start(value):
    d = _utc(value).replace(second=0, microsecond=0)
    return d.replace(minute=d.minute - d.minute % config.RAW_SIGNAL_DISCORD_BATCH_MINUTES)

def capture(event, db_path=None):
    """Best effort only: this function cannot affect alpha or intent delivery."""
    try:
        event = event if "entry_policy" in event else annotate_candidate(event)
        observed = _utc(event["observed_at"])
        material = "|".join(str(event.get(k, "")) for k in (
            "strategy_id", "plugin_version", "asset", "direction", "observed_at",
            "input_snapshot_id",
        ))
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
        persist_observation(conn, event)
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

def render(rows, start, skipped_symbols=0):
    end = start + timedelta(minutes=config.RAW_SIGNAL_DISCORD_BATCH_MINUTES)
    lines = [f"📊 SIGNAL · research-analyst · {config.RAW_SIGNAL_DISCORD_BATCH_MINUTES}m",
             f"window {start:%H:%M}–{end:%H:%M} UTC", "```",
             "asset  side   strat                    gate/clash",
             "─────  ─────  ───────────────────────  ───────────"]
    for row in rows[:5]:
        lines.append(f"{row[3]:<5}  {row[4].upper():<5}  {row[2]:<23}  {row[8] or 'pending'}/{row[10] or 'pending'}")
    lines.append("```")
    remaining = max(0, len(rows) - 5)
    if remaining:
        lines.append(f"+ {remaining} more signal evaluations")
    lines.append("Research-only observation; no execution, fills, or orders are implied.")
    lines.append(f"skipped {max(0, skipped_symbols)} symbols (observed)")
    return "\n".join(lines)

def publish_once(now=None, db_path=None, transport=None):
    if not getattr(config, "RAW_SIGNAL_DISCORD_BATCH_ENABLED", False): return False
    now = _utc(now or datetime.now(timezone.utc)); end = window_start(now)
    start = end - timedelta(minutes=config.RAW_SIGNAL_DISCORD_BATCH_MINUTES)
    key = start.isoformat().replace("+00:00", "Z")
    conn = config.get_db_connection(db_path=db_path or config.ANALYST_DB_PATH)
    try:
        batch = conn.execute("SELECT status, attempts, message_text FROM discord_signal_batches WHERE window_start = ?", (key,)).fetchone()
        if batch is not None:
            if batch[0] not in {"pending", "claimed"}:
                return False
            rows = conn.execute("""SELECT r.raw_signal_id,r.candidate_id,r.strategy_id,r.asset,r.direction,
                        r.observed_at,r.payload_json,h.executor_intent_status,h.hard_gate_status,
                        h.executor_intent_status,h.clash_status
                      FROM discord_signal_batch_members m JOIN raw_signals r ON r.raw_signal_id = m.raw_signal_id
                      LEFT JOIN raw_signal_status_history h ON h.status_id = (
                        SELECT status_id FROM raw_signal_status_history WHERE raw_signal_id=r.raw_signal_id
                        ORDER BY recorded_at DESC LIMIT 1)
                      WHERE m.window_start = ? ORDER BY r.observed_at,r.raw_signal_id""", (key,)).fetchall()
        else:
            rows = conn.execute("""SELECT r.raw_signal_id,r.candidate_id,r.strategy_id,r.asset,r.direction,
                    r.observed_at,r.payload_json,h.executor_intent_status,h.hard_gate_status,
                    h.executor_intent_status,h.clash_status
                     FROM raw_signals r LEFT JOIN raw_signal_status_history h ON h.status_id = (
                       SELECT status_id FROM raw_signal_status_history WHERE raw_signal_id=r.raw_signal_id
                       ORDER BY recorded_at DESC LIMIT 1)
                     WHERE r.observed_at < ? AND
                       ((r.observed_at >= ? AND r.observed_at < ?) OR NOT EXISTS (
                          SELECT 1 FROM discord_signal_batch_members m
                          WHERE m.raw_signal_id = r.raw_signal_id
                        ))
                     ORDER BY r.observed_at,r.raw_signal_id""", (end.isoformat(), start.isoformat(), end.isoformat())).fetchall()
        if not rows: return False
        now_s = now.isoformat()
        conn.execute("INSERT OR IGNORE INTO discord_signal_batches(window_start,window_end,status,candidate_count,message_count) VALUES (?, ?, 'pending', ?, 0)", (key, end.isoformat(), len(rows)))
        conn.commit()
        claim_id = str(uuid4())
        stale_before = (now - timedelta(seconds=config.RAW_BATCH_CLAIM_LEASE_SECONDS)).isoformat()
        backoff = config.RAW_BATCH_RETRY_BACKOFF_SECONDS * (2 ** max((batch[1] if batch else 0) - 1, 0))
        retry_before = (now - timedelta(seconds=backoff)).isoformat()
        claimed = conn.execute(
            """UPDATE discord_signal_batches
               SET status='claimed',claimed_at=?,claimed_by=?,attempts=attempts+1
               WHERE window_start=? AND attempts < ? AND
                 ((status='pending' AND (claimed_at IS NULL OR claimed_at < ?)) OR
                  (status='claimed' AND claimed_at < ?))""",
            (now_s, claim_id, key, config.RAW_BATCH_MAX_ATTEMPTS, retry_before, stale_before),
        ).rowcount
        conn.commit()
        if not claimed: return False
        if batch is None:
            conn.executemany(
                "INSERT OR IGNORE INTO discord_signal_batch_members(window_start,raw_signal_id) VALUES (?, ?)",
                [(key, row[0]) for row in rows],
            )
        conn.commit()
    finally: conn.close()
    try:
        observed_assets = {row[3] for row in rows}
        skipped = len(set(config.load_static_symbols()) - observed_assets)
        message = batch[2] if batch and batch[2] else render(rows, start, skipped)
        if not (batch and batch[2]):
            conn = config.get_db_connection(db_path=db_path or config.ANALYST_DB_PATH)
            conn.execute("UPDATE discord_signal_batches SET message_text=?,message_hash=? WHERE window_start=? AND status='claimed' AND claimed_by=?", (message, hashlib.sha256(message.encode()).hexdigest(), key, claim_id))
            conn.commit(); conn.close()
        response = (transport or DiscordWebhookTransport(config.RAW_SIGNAL_DISCORD_WEBHOOK_URL)).send(message)
        conn = config.get_db_connection(db_path=db_path or config.ANALYST_DB_PATH)
        conn.execute("UPDATE discord_signal_batches SET status='sent',sent_at=?,response_body=?,message_count=1 WHERE window_start=? AND status='claimed' AND claimed_by=?", (datetime.now(timezone.utc).isoformat(), response, key, claim_id))
        conn.commit(); conn.close(); return True
    except Exception as exc:
        conn = config.get_db_connection(db_path=db_path or config.ANALYST_DB_PATH)
        conn.execute("UPDATE discord_signal_batches SET status=CASE WHEN attempts >= ? THEN 'failed' ELSE 'pending' END,error_message=? WHERE window_start=? AND status='claimed' AND claimed_by=?", (config.RAW_BATCH_MAX_ATTEMPTS, str(exc)[:500], key, claim_id))
        conn.commit(); conn.close(); return False
