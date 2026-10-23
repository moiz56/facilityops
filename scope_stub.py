"""A quick look at tenant scoping: what each tenant resolves to, and what is refused.

    PYTHONPATH=src python scope_stub.py
"""

from common import PROJECT_ROOT
from common.paths import load_config
from query.scope import QueryContext, ScopeError, resolve

TENANTS = load_config(PROJECT_ROOT / "config" / "tenants.yaml")


def context(tenant_id, facility_ids=(), run_window=None):
    return QueryContext(tenant_id, tuple(facility_ids), run_window, "stub", "2026-10-23T00:00:00Z")


def show(label, ctx):
    print(f"\n{label}")
    try:
        records = resolve(ctx, TENANTS, PROJECT_ROOT)
    except ScopeError as error:
        print(f"  refused: {error}")
        return
    print(f"  {len(records)} runs, facilities {sorted({r.facility_id for r in records})}")
    if records:
        print(f"  first {records[0].run_id}")
        print(f"  last  {records[-1].run_id}")


show("tenant_facility, all its facilities", context("tenant_facility"))
show("tenant_racks, all its facilities", context("tenant_racks"))
show("tenant_racks, window 2026-08-01 to 2026-08-05", context("tenant_racks", run_window=("2026-08-01", "2026-08-05")))
show("tenant_facility asking for the racks facility", context("tenant_facility", ["sis_racks_checkpoint_route"]))
show("a facility that does not exist", context("tenant_facility", ["no_such_facility"]))
show("unknown tenant", context("tenant_nobody"))
show("bad run window", context("tenant_racks", run_window=("2026-08-01", "not a date")))
