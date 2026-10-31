"""The first-step router: lookup, derivation or abstain, tried with semantic-router.

Standalone: imports nothing from the project. Run it from the project root:

    python src/router/first_step_router.py                  # test table before and after fit, then save
    python src/router/first_step_router.py "your question"  # load the saved router, route one question

The saved router (first_step_router.yaml, beside this file) holds the routes,
the encoder name, the threshold and the fitted calibration. Change the
question sets, their labels or the hand-written lists and it is out of date:
run without a question again to refit.

  lookup     a value exactly as recorded, for a named checkpoint, zone or run
  derivation a figure worked out over the runs: a count, share, average, ranking
  abstain    a question the records cannot answer: why, what next, better or worse

A question below THRESHOLD gets no route, which is treated as abstain too.

The routes are trained on the question sets (config/b1_question_set.yaml and
config/b1_question_set_extension.yaml), labelled in question_set_labels.yaml
(router.label_question_set). Each question is split, so that its wordings are
never shared between the three sets:
  the question and its first 2 paraphrases   route utterances
  the next paraphrase                        calibration
  the rest                                   tests
A catalogue question ("what can I ask") has no route here and is left out.
The hand-written ROUTES, CALIBRATION and TESTS below are used only for a route
the question sets give no question to, and for their off-topic (None)
questions, which teach the calibration what a low score looks like.

The utterances name no real checkpoint or zone: they say <checkpoint-id> and
<zone-id>. Every question goes through normalise() first, which swaps a name
for the same placeholder, so "was a3_back completed" is matched as
"was <checkpoint-id> completed". It knows these shapes only:
  checkpoints  checkpoint_4, checkpoint 4, cp4, and the racks route's a3_back,
               c5_front, ups_front
  zones        the racks route's rowA_back, rowC_front
Names like home or ac_1 are both a zone and a checkpoint, so they are left as typed.

Three sets of questions, kept apart on purpose:
  routes       the utterances the router matches against
  calibration  labelled questions fit() uses to turn scores into probabilities
  tests        the questions the router is checked on, seen by neither
"""

import re
import sys
from pathlib import Path

import yaml
from semantic_router import IndexedRouter, Route, load_router
from semantic_router.encoders import FastEmbedEncoder

ENCODER = "BAAI/bge-small-en-v1.5"   # local model, no API key
THRESHOLD = 0.6                      # below this the question gets no route
HERE = Path(__file__).resolve().parent
ROUTER_FILE = HERE / "first_step_router.yaml"

CONFIG = HERE.parents[1] / "config"
QUESTION_SETS = (CONFIG / "b1_question_set.yaml", CONFIG / "b1_question_set_extension.yaml")
LABELS_FILE = HERE / "question_set_labels.yaml"
ROUTE_EXAMPLES = 3         # the question and its first 2 paraphrases
CALIBRATION_EXAMPLES = 1   # the paraphrase after those; the rest are tests

# checkpoint_4, checkpoint 4, cp4, cp 4, a3_back, c5_front, ups_front
CHECKPOINT = re.compile(
    r"\b(?:checkpoint[_ ]?\d+|cp ?\d+|[a-c]\d+_(?:front|back)|ups_(?:front|back))\b", re.IGNORECASE,
)
# rowA_back, rowC_front
ZONE = re.compile(r"\brow[a-z]_(?:front|back)\b", re.IGNORECASE)


def normalise(question: str) -> str:
    """The question with each checkpoint and zone name swapped for its placeholder."""
    return ZONE.sub("<zone-id>", CHECKPOINT.sub("<checkpoint-id>", question))


def split_question_set() -> list[dict]:
    """Each labelled question in the question sets, split three ways.

    {id, first_step, second_step, routes, calibration, tests}, in file order;
    catalogue questions are left out. Exits when the labels file is missing or
    a question has no label.
    """
    if not LABELS_FILE.exists():
        sys.exit(f"{LABELS_FILE} not found: run PYTHONPATH=src python -m router.label_question_set first")
    labels = {row["id"]: row for row in yaml.safe_load(LABELS_FILE.read_text(encoding="utf-8"))["labels"]}
    split = []
    for path in QUESTION_SETS:
        for entry in yaml.safe_load(path.read_text(encoding="utf-8"))["questions"]:
            qid = str(entry["id"]).split()[0]   # some ids carry a note, e.g. "Q21 (ALREADY IMPLEMENTED)"
            if qid not in labels:
                sys.exit(f"{qid} has no label in {LABELS_FILE.name}: delete it and label again")
            label = labels[qid]
            if label["first_step"] == "catalogue":
                continue
            texts = [entry["question"].strip()] + [p.strip() for p in entry.get("paraphrases") or []]
            calibration_end = ROUTE_EXAMPLES + CALIBRATION_EXAMPLES
            split.append({
                "id": qid, "first_step": label["first_step"], "second_step": label["second_step"],
                "routes": texts[:ROUTE_EXAMPLES], "calibration": texts[ROUTE_EXAMPLES:calibration_end],
                "tests": texts[calibration_end:],
            })
    return split


def build_sets(
    labelled: list[tuple[str, list[str], list[str], list[str]]], hand_routes: dict[str, list[str]],
    hand_calibration: list[tuple[str, str | None]], hand_tests: list[tuple[str, str | None]],
) -> tuple[dict[str, list[str]], list[tuple[str, str | None]], list[tuple[str, str | None]], list[str]]:
    """Routes, calibration and tests from labelled questions: (route, routes, calibration, tests) each.

    Every route in hand_routes is kept. One the labelled questions give
    nothing to keeps its hand-written utterances, calibration and tests; its
    name is in the list returned last. The hand-written off-topic (None)
    questions are always added.
    """
    routes: dict[str, list[str]] = {name: [] for name in hand_routes}
    calibration: list[tuple[str, str | None]] = []
    tests: list[tuple[str, str | None]] = []
    for route, examples, calibrating, testing in labelled:
        if route not in routes:
            sys.exit(f"{LABELS_FILE.name}: '{route}' is not a route; the routes are {', '.join(routes)}")
        # Utterances are matched in their normalised form, so they are stored that way.
        routes[route] += [normalise(text) for text in examples]
        calibration += [(text, route) for text in calibrating]
        tests += [(text, route) for text in testing]

    hand_written = [name for name, utterances in routes.items() if not utterances]
    for name in hand_written:
        routes[name] = hand_routes[name]
        calibration += [(q, label) for q, label in hand_calibration if label == name]
        tests += [(q, label) for q, label in hand_tests if label == name]
    calibration += [(q, label) for q, label in hand_calibration if label is None]
    tests += [(q, label) for q, label in hand_tests if label is None]
    return routes, calibration, tests, hand_written


# Hand-written: used only for a route the question sets give nothing to.
ROUTES = {
    # A value as recorded: one checkpoint, zone or run, read straight off the record.
    "lookup": [
        "what did <checkpoint-id> look like on the last run",
        "when was <checkpoint-id> last checked and what did it find",
        "pull up the latest record for <checkpoint-id>",
        "did the robot actually get to <checkpoint-id> this morning",
        "what's the status of <checkpoint-id> on the most recent run",
        "did <checkpoint-id> pass",
        "what was observed at <checkpoint-id>",
        "can you show me the notes left on <checkpoint-id>",
        "are there any findings logged against <checkpoint-id>",
        "what temperature did <checkpoint-id> read last time",
        "give me the vibration numbers for <checkpoint-id>",
        "what were the air quality readings at <checkpoint-id>",
        "why is there no humidity value for <checkpoint-id>",
        "everything the sensors picked up at <checkpoint-id> please",
        "show me the pictures from <checkpoint-id>",
        "is there a thermal image of <checkpoint-id>",
        "what evidence do we have for <checkpoint-id> from the latest run",
        "where are the photos for <checkpoint-id> stored",
        "which zone does <checkpoint-id> sit in",
        "what checkpoints are in the same zone as <checkpoint-id>",
        "list the stops in <zone-id> in route order",
        "what comes after <checkpoint-id> on the route",
        "which zones did the robot go through on the last run",
        "what was the result at <checkpoint-id> on 19 august",
        "what happened at <checkpoint-id> on yesterday afternoon's run",
        "was <checkpoint-id> skipped on the latest run",
        "what's the expected condition written down for <checkpoint-id>",
        "what time was <checkpoint-id> inspected",
        "status and temperature for <checkpoint-id>, latest run",
        "hey what did the last inspection say about <checkpoint-id>",
    ],
    # A figure worked out over checkpoints or runs: counts, shares, averages,
    # rankings, what differs between runs, what was missed.
    "derivation": [
        "how many checkpoints failed on the latest run out of the total",
        "which checkpoints went over the temperature limit",
        "by how much did each stop exceed the temp threshold",
        "what percent of the route got finished last time",
        "which run had the best completion rate",
        "average temperature for each zone on the last run",
        "which area had the most vibration on average",
        "where was the peak temperature, which zone and which checkpoint",
        "top five hottest readings from the latest run",
        "give me the five biggest vibration readings",
        "how many findings are there by status, logged vs needs review",
        "how many sensor alerts went off, split by severity",
        "what's different between the last two runs",
        "did the route change from the previous run",
        "which checkpoints are new compared to the run before",
        "which stops failed on both of the last two runs",
        "what got missed on the latest run and why",
        "has anything been missed more than once",
        "which sensors were unavailable on the last run and why",
        "how many runs do we have on record and over what dates",
        "which day had the most inspection runs",
        "how often does the robot do this route",
        "list the runs between 14 and 21 august",
        "were there any runs over the weekend",
        "how many images actually resolved out of the ones referenced",
        "which checkpoints have no photos at all",
        "was there anything we captured but couldn't read last run",
        "how many findings were flagged abstained, and where",
        "which checkpoints are still waiting on review",
        "which run had the most failures",
    ],
    # The records cannot answer it: a cause, a forecast, a judgement, advice,
    # or a fact an inspection record never holds.
    "abstain": [
        "why did the temperature jump at <checkpoint-id> between runs",
        "what's causing the vibration in <zone-id>",
        "what's behind the high readings at <checkpoint-id>",
        "why was the particulate sensor broken at <checkpoint-id>",
        "what went wrong with the robot on tuesday",
        "is the equipment near <checkpoint-id> going to break down",
        "when is the next failure likely to happen",
        "will the temperature keep going up next week",
        "which zone should we expect trouble from next",
        "how long until <checkpoint-id> needs replacing",
        "who makes the sensor hub",
        "how long has this route been running for",
        "when was this facility built",
        "what firmware is the robot on",
        "who is the technician responsible for <zone-id>",
        "is the warranty still valid on the hvac units",
        "how much power does the facility use in a month",
        "what model of camera does the robot use",
        "is the latest route better than the old one",
        "should we be worried about <checkpoint-id>",
        "is the vibration at <checkpoint-id> dangerous",
        "is this site going to pass its audit",
        "how healthy is the facility overall, give it a score",
        "are the readings in the server room acceptable",
        "what should maintenance fix first",
        "do we need to send someone out to <checkpoint-id>",
        "how can we bring the temperature down in <zone-id>",
        "can you schedule a reinspection for friday",
        "recommend a better route for the robot",
        "what would happen if we skipped <checkpoint-id> entirely",
    ],
}

# Hand-written labelled questions fit() learns from, used for a hand-written
# route and for the off-topic ones: None means no route should match, so the
# fitted curve learns what a low score looks like.
CALIBRATION = [
    ("what did checkpoint_6 record for pressure", "lookup"),
    ("did b2_front get visited on the latest run", "lookup"),
    ("can i see the thermal shots from cp3", "lookup"),
    ("which zone is ups_front part of", "lookup"),
    ("what notes did the operator leave at a2_back", "lookup"),
    ("what's recorded as observed at c3_front last time", "lookup"),
    ("was b4_back pass or fail on the 4th of august", "lookup"),
    ("give me the particulate numbers at a5_front", "lookup"),
    ("why is the accelerometer blank for b6_back", "lookup"),
    ("show the evidence list for c1_back", "lookup"),
    ("what's the next stop after a4_front", "lookup"),
    ("when did the robot reach c2_back on the last run", "lookup"),
    ("how many stops did the robot miss last time", "derivation"),
    ("which checkpoints ran hotter than the limit on the last run", "derivation"),
    ("what share of the checkpoints got completed this morning", "derivation"),
    ("mean temperature by zone please", "derivation"),
    ("which zone peaked highest for vibration", "derivation"),
    ("five highest temperature readings on the latest run", "derivation"),
    ("what changed between the last run and the one before", "derivation"),
    ("which checkpoints keep getting missed", "derivation"),
    ("how many runs have there been since august", "derivation"),
    ("break the findings down by status", "derivation"),
    ("which particulate sensors were offline on the latest run", "derivation"),
    ("how many of the photos are actually there", "derivation"),
    ("why does rowB_front keep running hot", "abstain"),
    ("what made the robot miss so many stops", "abstain"),
    ("is b3_front about to fail", "abstain"),
    ("what will the vibration look like next month", "abstain"),
    ("who installed the racks", "abstain"),
    ("how old is the air conditioning unit", "abstain"),
    ("is the dust level at c4_back a health risk", "abstain"),
    ("rate the condition of the facility out of ten", "abstain"),
    ("what should we replace first", "abstain"),
    ("can you order a new sensor for a6_front", "abstain"),
    ("is the new route an improvement", "abstain"),
    ("what's the serial number of the robot", "abstain"),
    ("what's the capital of france", None),
    ("write me a poem about robots", None),
    ("what's the weather like today", None),
    ("how do i reset my password", None),
    ("translate good morning into spanish", None),
    ("who won the football last night", None),
    ("recommend a good pizza place nearby", None),
    ("what's 17 times 23", None),
]

# Hand-written tests on sis_racks names, used for a hand-written route and for
# the off-topic (None) ones.
TESTS = [
    ("what readings were taken at c5_front on 30 july", "lookup"),
    ("what was a3_back's verdict last run", "lookup"),
    ("which zone is b5_front in", "lookup"),
    ("show me the images from a1_back on the 19th of august", "lookup"),
    ("what temperature did c2_front read in the latest run", "lookup"),
    ("any IR shots taken at b1_back", "lookup"),
    ("what was observed at a6_back on 4 august", "lookup"),
    ("what order does the robot visit the stops in rowA_back", "lookup"),
    ("how come a3_back has no pm2.5 reading", "lookup"),
    ("which zone does the robot head to after rowB_front", "lookup"),
    ("how many checkpoints were missed on the 30 july runs", "derivation"),
    ("which checkpoints in rowC_back went over the temperature limit", "derivation"),
    ("what percentage of the route was completed on 18 august", "derivation"),
    ("average vibration per zone in the latest run", "derivation"),
    ("which checkpoint had the highest temperature in rowA_front", "derivation"),
    ("rank the highest vibration readings from the latest run, top 5", "derivation"),
    ("which stops were dropped from the route since the previous run", "derivation"),
    ("is any checkpoint missed on several runs", "derivation"),
    ("over what dates were the runs recorded", "derivation"),
    ("how many findings need someone to look at them on the latest run", "derivation"),
    ("why is rowC_front hotter than rowA_front", "abstain"),
    ("will b2_back fail on the next run", "abstain"),
    ("who manufactured the particulate sensor", "abstain"),
    ("how long has the racks route existed", "abstain"),
    ("is the vibration at a4_back something to worry about", "abstain"),
    ("should we move the robot's docking station", "abstain"),
    ("what will it cost to cool down rowB_back", "abstain"),
    ("is the server room safe to work in", "abstain"),
    ("is today's route better than last week's", "abstain"),
    ("what caused the sensor faults in august", "abstain"),
    ("tell me a joke", None),
    ("book a meeting for monday", None),
    ("what time is it in tokyo", None),
    ("help me write an email to my manager", None),
    ("whats the best laptop for coding", None),
]


def build_router(routes: dict[str, list[str]]) -> IndexedRouter:
    return IndexedRouter(
        encoder=FastEmbedEncoder(name=ENCODER),
        routes=[Route(name=name, utterances=utterances) for name, utterances in routes.items()],
    )


def run_tests(router: IndexedRouter, tests: list[tuple[str, str | None]], title: str) -> None:
    print(f"\n{title}")
    print(f"{'question':<48} {'expected':<10} {'got':<10} {'conf':>5}  ok")
    passed = 0
    for question, expected in tests:
        choice = router(normalise(question))
        got = choice.name
        # no route and the abstain route both mean the records cannot answer it
        ok = got == expected or (expected in ("abstain", None) and got in ("abstain", None))
        passed += ok
        confidence = f"{choice.confidence:.2f}" if choice.confidence is not None else "-"
        print(f"{question[:47]:<48} {str(expected):<10} {str(got):<10} {confidence:>5}  {'yes' if ok else 'NO'}")
    print(f"{passed} of {len(tests)} passed")


def calibrate(router: IndexedRouter, calibration: list[tuple[str, str | None]]) -> None:
    questions = [normalise(question) for question, _ in calibration]
    labels = [label for _, label in calibration]
    report = router.fit(X=questions, y=labels)
    print(f"fit: {report.n} questions, accuracy {report.accuracy:.2f}, calibration error {report.ece:.2f}")
    router.confidence_threshold = THRESHOLD


def main() -> None:
    # A question on the command line: load the calibrated router and route
    # just that one.
    if len(sys.argv) > 1:
        if not ROUTER_FILE.exists():
            sys.exit(f"{ROUTER_FILE} not found: run without a question first to calibrate")
        router = load_router(ROUTER_FILE)
        question = " ".join(sys.argv[1:])
        routed = normalise(question)
        choice = router(routed)
        confidence = f"{choice.confidence:.2f}" if choice.confidence is not None else "-"
        print(f"{question}\n  routed as: {routed}\n  route: {choice.name}  confidence: {confidence}  "
              f"(threshold {router.confidence_threshold})")
        # Every route, best first, whatever its confidence. Each is the fitted
        # curve applied to that route's own score, so they need not add up to 1.
        print("  every route:")
        for each in router(routed, limit=None, threshold=float("-inf")):
            print(f"    {each.name:<12} {each.confidence:.2f}")
        return

    split = split_question_set()
    routes, calibration, tests, hand_written = build_sets(
        [(q["first_step"], q["routes"], q["calibration"], q["tests"]) for q in split], ROUTES, CALIBRATION, TESTS,
    )
    print(f"{len(split)} questions: " + ", ".join(f"{name} {len(u)} utterances" for name, u in routes.items())
          + f"; {len(calibration)} calibration, {len(tests)} tests")
    if hand_written:
        print(f"hand-written, as the question sets give them nothing: {', '.join(hand_written)}")
    router = build_router(routes)

    # Before fit: confidence is the raw similarity, and with no threshold the
    # best match always wins, so None never comes back.
    run_tests(router, tests, "before fit (raw scores, no threshold)")

    print()
    calibrate(router, calibration)
    run_tests(router, tests, f"after fit (probabilities, threshold {THRESHOLD})")

    router.to_yaml(ROUTER_FILE)
    print(f"\ncalibrated router saved to {ROUTER_FILE}")


if __name__ == "__main__":
    main()
