# Repository Cleanup v1

Status: Phases 1-3 implemented; documentation archive remains incremental

## Objective

Reduce Research Analyst to the runtime described by `AGENTS.md`: five managed
services, cutoff-bound strategy evaluation, deterministic admission, advisory
raw-signal batches, and shared SQLite intent-bus publication. Historical code
and configuration must not look like live production capability.

The cleanup must not alter `.env`, production databases, executor files, or
managed-service state.

## Sources of truth

1. `AGENTS.md` owns the runtime and safety contract.
2. `src/research_analyst/config.py` owns runtime defaults.
3. `.env.example` contains deployment-only overrides and secrets placeholders;
   it must not restate every default.
4. `README.md` explains operator usage.
5. Superseded design material belongs under `specs/archive/` and cannot be
   treated as current configuration.

## Cleanup rules

- One setting has one canonical name. Remove compatibility aliases after their
  callers are migrated.
- Do not keep constants that are read only by tests or prose.
- Mandatory gates are code invariants, not feature flags.
- Retired delivery mechanisms must be removed as a complete vertical slice:
  code, config, schema creation, CLI output, tests, dependencies, and docs.
- Existing SQLite columns/tables are not dropped online. Stop creating or using
  retired schema first; destructive migration is a separate audited change.
- Strategy implementations retained for replay remain registered but are not
  mixed into the live production set.

## Phases

### Phase 1 — configuration convergence

- Align the watchlist default with the 160-symbol production contract.
- Keep the seven vectorbt ports as the sole default strategy allowlist.
- Reduce `.env.example` to opt-in switches, deployment paths, and secret
  placeholders.
- Remove dead aliases and constants, including the rotation cadence alias,
  test-only asset sets, unused BB warmup setting, unused LLM base URL, and the
  structural-admission toggle for the mandatory gate.
- Make the 12 legacy-production IDs and retired-research IDs disjoint.
- Centralize intent-bus package, WebSocket staleness, and resampling settings in
  `config.py`; remove the legacy ingest-cadence fallback and cross-channel
  notification-setting fallbacks.
- Remove the duplicate `INTENT_DELIVERY_ENABLED` gate and free-form
  `INTENT_ROUTING`; the bus path, target switches, and canonical strategy sets
  are the sole publication and account-policy controls.

### Phase 2 — retired runtime paths

- The unused orchestrator-owned market-pruning helper and its test-only surface
  were removed in the first cleanup slice; market retention remains owned by
  the gateway through `db_maintenance.prune_market_db`.
- The filesystem `execution_adapter` path and its `EXECUTION_*` config are
  removed.
- `intent_publisher` now owns durable alpha persistence and shared-bus retry;
  raw-signal Discord remains the only Research Analyst notification surface.
- The disabled analyst-local LLM workflow, configuration, schema, CLI, tests,
  and dependencies are removed.
- The unused legacy confluence-alert and market-pruning blocks are removed from
  the orchestrator.

### Phase 3 — schema and dependency retirement

- Fresh schemas no longer create or maintain retired notification-delivery,
  venue-delivery, confluence-alert, or LLM-research tables. Existing production
  tables are intentionally not dropped online.
- CLI branches for retired research and filesystem-delivery records are removed.
- Runtime requirements contain only directly imported packages.

### Phase 4 — documentation archive

- Replace duplicate `agent.md` content with a pointer to `AGENTS.md`.
- Rewrite or archive `docs/DESIGN.md`.
- Move explicitly superseded HTF, structural-admission, adapter, and retired
  strategy specs into `specs/archive/` without deleting audit history.

## Acceptance criteria

- Importing config exposes no compatibility-only or test-only setting.
- Copying `.env.example` does not change the default strategy set, retention
  policy, or watchlist size.
- The live orchestrator has no filesystem executor delivery, analyst-local LLM,
  or PM behavior.
- `python3 -m compileall -q src tests` succeeds.
- The test suite passes when the declared shared intent-bus development
  dependency is available.
