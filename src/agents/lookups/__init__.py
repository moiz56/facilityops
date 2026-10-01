"""B-1's SQL writer prompts for lookups, one module per lookup table.

The lookup router (b1_analytical_router_lookup) names the tables a question
needs; each table's module here holds the prompt the SQL writer uses to ask
the model for one SELECT over the lookup database. Every module has the same
parts as a derivation route (routes/): TYPE, TABLES, SUMMARY_SQL, PER_RUN and
PROMPT, with the same placeholders, and may add TEXT_COLUMNS: columns shown as
code wrote them, uncited (b1_analytical.build_lines).
"""

from agents.lookups import checkpoint_readings, checkpoints, evidence, zones

# Lookup table -> the module holding its SQL writer prompt.
PROMPTS = {
    checkpoints.TYPE: checkpoints,
    evidence.TYPE: evidence,
    checkpoint_readings.TYPE: checkpoint_readings,
    zones.TYPE: zones,
}
