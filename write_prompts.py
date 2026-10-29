"""Write each B-1 router's prompt, as built for one tenant's records, to prompts/<router>.txt.

The prompts are built the way the routers build them, with the tenant's runs,
checkpoints and zones filled in, but {question} is left as it is and there is
no feedback, so the files show the prompt a first attempt sends. Nothing calls
the model. Run it again after changing a prompt to see the new one.

    PYTHONPATH=src python write_prompts.py                          # tenant_facility
    PYTHONPATH=src python write_prompts.py --tenant tenant_racks    # another tenant
"""

import argparse
from datetime import datetime, timezone

from agents import b1_analytical_router_derived, b1_analytical_router_lookup, b1_retrieval_router
from common import PROJECT_ROOT
from query.ask import load_settings
from query.scope import QueryContext, resolve

OUTPUT_DIR = PROJECT_ROOT / "prompts"

parser = argparse.ArgumentParser(description="Write each router's built prompt for one tenant.")
parser.add_argument("--tenant", default="tenant_facility", help="the tenant, as named in config/tenants.yaml")
args = parser.parse_args()

settings = load_settings(PROJECT_ROOT)
context = QueryContext(
    tenant_id=args.tenant,
    facility_ids=(),
    run_window=None,
    asked_by="write_prompts",
    asked_at=datetime.now(timezone.utc),
)
records = resolve(context, settings.tenants, settings.root)
question = "{question}"

prompts = {
    "retrieval_router": b1_retrieval_router.build_prompt(question, "", settings.derivation),
    "router_derived": b1_analytical_router_derived.build_prompt(question, "", records, settings.derivation),
    "router_lookup": b1_analytical_router_lookup.build_prompt(question, "", records),
}

OUTPUT_DIR.mkdir(exist_ok=True)
for name, prompt in prompts.items():
    (OUTPUT_DIR / f"{name}.txt").write_text(prompt, encoding="utf-8")
    print(f"{name}  ->  prompts/{name}.txt")
