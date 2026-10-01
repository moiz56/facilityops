"""For each question in config/b1_question_set.yaml, print what the first
router (retrieval: derivation, lookup or abstain) output, then what the
second router (derivation or lookup, whichever the first picked) output.

    PYTHONPATH=src python stub.py                 # the 20 canonical questions
    PYTHONPATH=src python stub.py --paraphrases   # and every paraphrase
"""

import argparse
import json
import sys
from dataclasses import asdict

from agents.b1_analytical_router_derived import RouteError, route_question
from agents.b1_analytical_router_lookup import route_lookup
from agents.b1_retrieval_router import RetrievalError, classify_question
from agents.data_agent import load_corpus
from agents.provider import ProviderError, make_provider
from agents.utils import b1_config, derivation_config, hash_configs, load_env, provider_config
from common import PROJECT_ROOT
from common.paths import load_config, setting

QUESTION_SET = PROJECT_ROOT / "config" / "b1_question_set.yaml"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--paraphrases", action="store_true", help="also run every paraphrase")
    args = parser.parse_args()

    load_env(PROJECT_ROOT / ".env")
    paths = load_config(PROJECT_ROOT / "config" / "agent_path.yaml")
    report = load_config(PROJECT_ROOT / "config" / "report.yaml")
    derivations = load_config(PROJECT_ROOT / "config" / "derivations.yaml")
    agents = load_config(PROJECT_ROOT / "config" / "agents.yaml")
    config = derivation_config(report, derivations, hash_configs(report, derivations))
    b1 = b1_config(agents)
    provider = make_provider(provider_config(agents))
    records = load_corpus(PROJECT_ROOT / setting(paths, "agent", "data_dir"), setting(paths, "agent", "record_patterns"))

    cases = []
    for q in load_config(QUESTION_SET)["questions"]:
        cases.append((q["id"], q["question"]))
        if args.paraphrases:
            cases += [(f"{q['id']}p{n}", p) for n, p in enumerate(q.get("paraphrases") or [], 1)]

    for case_id, question in cases:
        print(f"\n=== {case_id}: {question}")
        try:
            retrieval = classify_question(question, provider, b1, config)
        except (ProviderError, RetrievalError) as error:
            print(f"first router: ERROR {error}")
            continue
        print(f"first router:  {json.dumps(asdict(retrieval))}")

        if retrieval.kind == "abstain":
            print("second router: (not run)")
            continue
        try:
            if retrieval.kind == "derivation":
                route = route_question(question, records, provider, b1, config)
            else:
                route = route_lookup(question, records, provider, b1)
        except (ProviderError, RouteError) as error:
            print(f"second router: ERROR {error}")
            continue
        print(f"second router ({retrieval.kind}): {json.dumps(asdict(route))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
