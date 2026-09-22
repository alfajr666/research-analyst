# Mechanical/Level Exit Fallback v1 — pointer

Normative spec lives at
`research-analyst-hl/specs/mechanical-exit-fallback-v1.md`
(single home; shared bus stays dumb and carries no copy).

RA implementation map: `config.py` (`INTENT_MECHANICAL_EXIT_FALLBACK_R`),
`trade_admission.py` + `intent_outbox.py` (pre-`admit()` fallback,
fingerprint over `exit_rule`), per-strategy `exit_class` records (§4 table),
`strategy_plugins.py` (`take_profit_mode` = native rule kind).
