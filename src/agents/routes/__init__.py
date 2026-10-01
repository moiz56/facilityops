"""B-1's SQL writer prompts, one module per derivation type.

The router (b1_analytical_router_derived) names the derivation instances a question
is about; b1_analytical then takes each instance's type to its module here and
asks the model for one SELECT with that module's PROMPT.

Every module has TYPE and PROMPT, and:
  TABLES, or tables(names, derivation) when its tables are one per instance
      (D4, D5, D6): what the model sees of the database
  SUMMARY_SQL, or summary_sql(names): the query code runs when the model's
      query returns no rows
  PER_RUN = False when its output is one for all runs (D7, D8), so its rows
      have no run_id

The router offers every type in PROMPTS (b1_analytical_router_derived.ROUTED_TYPES).
"""

from agents.routes import (
    condition_count, group_max, group_mean, proportion, rank_top_n, records, run_date_range, run_set_difference,
    threshold_compare,
)

# Route -> the module holding its SQL writer prompt: each derivation type, and
# records, the route for what the runs recorded.
PROMPTS = {
    records.TYPE: records,
    threshold_compare.TYPE: threshold_compare,
    condition_count.TYPE: condition_count,
    proportion.TYPE: proportion,
    group_mean.TYPE: group_mean,
    group_max.TYPE: group_max,
    rank_top_n.TYPE: rank_top_n,
    run_set_difference.TYPE: run_set_difference,
    run_date_range.TYPE: run_date_range,
}
