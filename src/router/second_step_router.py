"""The second-step router: which derivation type, or which lookup table, a
question needs once the first step has picked derivation or lookup.

Two routers, one per branch of the first step:

  derivation  one route per derivations.yaml type, plus records (what a run
              wrote down, as written). The model's router picks instances,
              e.g. group_mean_temperature_c; this one picks only the type,
              group_mean, so the reading is not its job.
  lookup      one route per lookup table: zones, checkpoints,
              checkpoint_readings, evidence.

Each router is trained on the question sets the way the first-step router
is (see first_step_router): the questions whose first-step label is that
branch, split into route utterances, calibration and tests, with their
second-step label as the route. A route the question sets give nothing to
keeps its hand-written utterances, calibration and tests below, and the
off-topic (None) questions are always added to calibration. Both are saved
beside this file: second_step_derivation.yaml and second_step_lookup.yaml.
Change the question sets, their labels or the hand-written lists and they are
out of date: run without a question again to refit.

A question below THRESHOLD (the first step's) gets no route.

Run it from the project root:

    PYTHONPATH=src python -m router.second_step_router                              # test before and after fit, then save
    PYTHONPATH=src python -m router.second_step_router derivation "your question"   # route one question
    PYTHONPATH=src python -m router.second_step_router lookup "your question"

Questions go through first_step_router's normalise() first, so names become
<checkpoint-id> and <zone-id> as they do there.

Three sets of questions per branch, kept apart on purpose:
  routes       the utterances the router matches against
  calibration  labelled questions fit() uses to turn scores into probabilities
  tests        the questions the router is checked on, seen by neither
"""

import sys
from pathlib import Path

from semantic_router import IndexedRouter, Route, load_router
from semantic_router.encoders import FastEmbedEncoder

from router.first_step_router import ENCODER, THRESHOLD, build_sets, normalise, split_question_set

HERE = Path(__file__).resolve().parent
ROUTER_FILES = {
    "derivation": HERE / "second_step_derivation.yaml",
    "lookup": HERE / "second_step_lookup.yaml",
}

# Hand-written: used only for a route the question sets give nothing to.
DERIVATION_ROUTES = {
    # D1: a reading against a configured limit
    "threshold_compare": [
        "which checkpoints went over the temperature limit on the last run",
        "did <checkpoint-id> go above the temp threshold",
        "how far over the limit was each stop",
        "did anything run hotter than 30 degrees",
        "which stops broke the vibration threshold",
        "list the checkpoints that stayed under the temperature limit",
        "by how much did <checkpoint-id> exceed the vibration limit",
        "how many checkpoints were over the heat threshold",
        "were any readings above the configured limit today",
        "which checkpoints in <zone-id> were over the threshold",
    ],
    # D2: items meeting a condition, or findings by status
    "condition_count": [
        "how many checkpoints failed on the latest run",
        "which stops failed and out of how many",
        "which run had the most failures",
        "list the failed checkpoints for each run",
        "how many findings are logged, needs review and abstained",
        "break down the findings by status",
        "which checkpoints were left out of the fail count and why",
        "count the fails on the runs from 31 august",
        "how many findings still need review",
        "which checkpoints have a fail result in <zone-id>",
    ],
    # D3: share of checkpoints completed
    "proportion": [
        "what percentage of checkpoints got completed last run",
        "what share of the route did the robot finish",
        "which run had the best completion rate",
        "completion rate for every run please",
        "how much of the route was covered on 19 august",
        "what fraction of stops were done this morning",
        "which stops did the robot actually finish on the first run",
        "how complete was the latest inspection",
        "is the completion rate the same across runs",
        "what proportion of checkpoints were completed in the most recent run",
    ],
    # D4: mean of a reading per zone
    "group_mean": [
        "average temperature per zone on the latest run",
        "mean vibration in <zone-id>",
        "what's the typical pm2.5 level in each zone",
        "which zone runs warmest on average",
        "average vibration by area for every run",
        "what was the mean temperature in <zone-id> and <zone-id>",
        "which zone has the highest average dust level",
        "what's the average reading in <checkpoint-id>'s zone",
        "mean temperature for each zone, per run",
        "which area has the lowest average vibration",
    ],
    # D5: highest reading per zone, and where it was
    "group_max": [
        "which zone had the highest peak temperature",
        "what was the hottest reading in <zone-id> and where",
        "where was the biggest vibration spike, zone and checkpoint",
        "which zone had the lowest peak vibration",
        "was <checkpoint-id> the hottest spot in its zone",
        "peak pm2.5 in each zone on the last run",
        "which run had the highest peak temperature",
        "max vibration per zone please",
        "what's the highest temperature recorded in each area",
        "which checkpoint gave the top reading in <zone-id>",
    ],
    # D6: the highest readings in a run, ranked
    "rank_top_n": [
        "top five vibration readings on the latest run",
        "what were the five hottest readings",
        "which three checkpoints had the most vibration",
        "did <checkpoint-id> make the top five for temperature",
        "list the ten highest vibration values",
        "rank the stops by pm2.5, highest first",
        "where did <checkpoint-id> rank for temperature",
        "the five worst dust readings last run",
        "top vibration and temperature readings together",
        "give me the highest readings in order",
    ],
    # D7: what is only in the previous run, only in the latest, or in both
    "run_set_difference": [
        "what's different between the last two runs",
        "did the route change since the previous run",
        "which checkpoints are new in the latest run",
        "was <checkpoint-id> on both of the last two routes",
        "how many stops were added and how many dropped",
        "which findings from last time aren't in this run",
        "which runs does the comparison cover",
        "which sensor blocks showed up in one run but not the other",
        "what checkpoints disappeared from the route",
        "which findings are new since the run before",
    ],
    # D8: when the runs were taken
    "run_date_range": [
        "over what period were the runs taken",
        "when was the first run and when was the latest",
        "how many runs are there and over how many days",
        "which day had the most runs",
        "which runs happened on 19 august and at what time",
        "how often does the robot go out",
        "were there any runs at the weekend",
        "list the runs between 14 and 21 august",
        "what dates do the records cover",
        "how many inspection runs have been recorded",
    ],
    # D9: sensor alerts, how many and of which severity
    "sensor_alert_count": [
        "how many sensor alerts were there on the last run",
        "alerts by severity please",
        "how many critical alerts fired",
        "count the warnings and criticals for each run",
        "which run had the most alerts",
        "were there any critical sensor alerts this week",
        "how many alerts did the robot raise",
        "breakdown of sensor warnings per run",
        "did any run have zero alerts",
        "how many warning level alerts are there",
    ],
    # D10: evidence images, how many, thermal or RGB, resolved or not
    "evidence_count": [
        "how many images were taken on the latest run",
        "how many photos resolved and how many are missing",
        "how many thermal images do we have",
        "thermal versus rgb image count per run",
        "how many pictures were captured at <checkpoint-id>",
        "which checkpoints have the most evidence",
        "what share of the images actually exist",
        "are any image files missing or broken",
        "evidence count for each checkpoint on the last run",
        "how many captures does each run have",
    ],
    # D11: whether the robot's declared counts match the run's lists
    "count_check": [
        "do the run's declared counts match the checkpoint list",
        "does the passed count add up",
        "is the robot's finding count right",
        "check the counts on the latest run",
        "do the totals the robot reported match what's in the record",
        "are the completed checkpoint numbers consistent",
        "does each event's evidence count match the visit",
        "any mismatches between the declared and actual counts",
        "is the missed count correct for this run",
        "do the summary numbers on the run agree with the details",
    ],
    # not a derivation: what each run wrote down, as written
    "records": [
        "what did the robot note at <checkpoint-id>",
        "show the event log for the latest run",
        "what findings were recorded on the last run",
        "what was the final status of the run",
        "list the sensor alerts with their descriptions",
        "what's the recommended action on the findings at <checkpoint-id>",
        "when did the run start and finish",
        "what was observed at <checkpoint-id> on the first run",
        "why is there no reading for <checkpoint-id>",
        "what did the run record about <zone-id>",
    ],
}

LOOKUP_ROUTES = {
    # the zones on the route, in order
    "zones": [
        "which zones did the route cover last run",
        "what zone comes after <zone-id>",
        "list the zones in route order",
        "what's the first zone the robot visits",
        "what areas does the route go through",
        "which zone is the last one before docking",
        "what zone comes before <zone-id>",
        "show me the zones for the latest run",
        "did the route go through <zone-id> on the last run",
        "what order are the zones visited in",
    ],
    # each checkpoint's status, verdict, zone and place on the route
    "checkpoints": [
        "was <checkpoint-id> completed last run",
        "which zone is <checkpoint-id> in",
        "what was <checkpoint-id>'s result on 19 august",
        "which checkpoints are in <zone-id> in route order",
        "did <checkpoint-id> pass or fail",
        "what's the status of every stop in <zone-id>",
        "was <checkpoint-id> missed this morning",
        "list the checkpoints on the route in order",
        "what comes after <checkpoint-id>",
        "what verdict did <checkpoint-id> get on each run",
    ],
    # each checkpoint's sensor readings, or why one is missing
    "checkpoint_readings": [
        "what was the temperature at <checkpoint-id> last run",
        "show the accelerometer values at <checkpoint-id>",
        "why is there no pm2.5 reading at <checkpoint-id>",
        "what did the sensors record at <checkpoint-id>",
        "humidity at <checkpoint-id> please",
        "what were the readings in <zone-id> on the latest run",
        "when was the vibration read at <checkpoint-id>",
        "was the particulate reading usable at <checkpoint-id>",
        "give me all the environment readings for <checkpoint-id>",
        "which readings were left out at <checkpoint-id> and why",
    ],
    # the images each checkpoint captured
    "evidence": [
        "which images were taken at <checkpoint-id>",
        "show the thermal images in <zone-id>",
        "are there any photos for <checkpoint-id>",
        "where are the pictures from <checkpoint-id> on the last run",
        "show me the rgb shots at <checkpoint-id>",
        "which images belong to the findings at <checkpoint-id>",
        "did the images at <checkpoint-id> load",
        "what evidence was captured in <zone-id>",
        "show the annotated images for <checkpoint-id>",
        "list every image from the latest run at <checkpoint-id>",
    ],
}

ROUTES = {"derivation": DERIVATION_ROUTES, "lookup": LOOKUP_ROUTES}

# Off-topic questions, labelled None in both branches: no route should match,
# so the fitted curve learns what a low score looks like.
OFF_TOPIC = [
    ("what's the capital of italy", None),
    ("write a haiku about servers", None),
    ("how do i change my email address", None),
    ("what's the exchange rate for euros", None),
    ("who painted the mona lisa", None),
    ("set a timer for ten minutes", None),
]

# Hand-written labelled questions fit() learns from, every route in each
# branch covered, using sis_racks names. Used for a hand-written route and
# for the off-topic ones.
CALIBRATION = {
    "derivation": [
        ("which stops in rowC_front went past the temperature limit", "threshold_compare"),
        ("was a2_front over the vibration threshold on 18 august", "threshold_compare"),
        ("how many readings broke the configured limit last run", "threshold_compare"),
        ("how many checkpoints got a fail verdict on 4 august", "condition_count"),
        ("count the findings that are still logged", "condition_count"),
        ("which run had the fewest failed stops", "condition_count"),
        ("what fraction of the route was finished on 30 july", "proportion"),
        ("completion percentage on the latest run", "proportion"),
        ("did every run complete the same share of checkpoints", "proportion"),
        ("average pm2.5 in rowB_back", "group_mean"),
        ("mean humidity per zone on 4 august", "group_mean"),
        ("which zone had the coolest average temperature", "group_mean"),
        ("highest vibration reading in each zone on 18 august", "group_max"),
        ("where was the hottest spot in rowC_back", "group_max"),
        ("which zone saw the biggest pm2.5 peak", "group_max"),
        ("rank the three hottest checkpoints on 19 august", "rank_top_n"),
        ("top ten dust readings, highest first", "rank_top_n"),
        ("was b6_front among the top vibration readings", "rank_top_n"),
        ("which checkpoints were on the previous run but not this one", "run_set_difference"),
        ("what findings showed up in both of the last two runs", "run_set_difference"),
        ("did c1_front get added to the route recently", "run_set_difference"),
        ("what's the date of the most recent run", "run_date_range"),
        ("how many runs were done on 30 july", "run_date_range"),
        ("list every run with its start time", "run_date_range"),
        ("how many warning alerts were raised on 4 august", "sensor_alert_count"),
        ("which run raised the fewest sensor alerts", "sensor_alert_count"),
        ("count the critical alerts across all runs", "sensor_alert_count"),
        ("how many rgb images were taken on 18 august", "evidence_count"),
        ("how many evidence files could not be found", "evidence_count"),
        ("which checkpoint has the fewest photos", "evidence_count"),
        ("does the completed count match the checkpoints listed on 30 july", "count_check"),
        ("is the declared missed total consistent with the record", "count_check"),
        ("do the robot's reported numbers line up for every run", "count_check"),
        ("what did the robot write at c6_back on 4 august", "records"),
        ("what recommended actions are on the latest findings", "records"),
        ("what time did the latest run end", "records"),
    ] + OFF_TOPIC,
    "lookup": [
        ("which zone does the robot start in", "zones"),
        ("what zone follows rowB_back", "zones"),
        ("list the areas the robot passed through on 4 august", "zones"),
        ("is rowC_front before or after rowB_front on the route", "zones"),
        ("what zones were on the 18 august run", "zones"),
        ("did a4_back pass on 30 july", "checkpoints"),
        ("what zone holds c6_front", "checkpoints"),
        ("was b3_back skipped on the latest run", "checkpoints"),
        ("which stops are in rowA_front", "checkpoints"),
        ("what's the stop after b5_back", "checkpoints"),
        ("what humidity did c4_front record on 4 august", "checkpoint_readings"),
        ("show the vibration values at a2_back", "checkpoint_readings"),
        ("why does b6_back have no temperature reading", "checkpoint_readings"),
        ("what were the dust levels at c1_back", "checkpoint_readings"),
        ("all sensor values for ups_front on the latest run", "checkpoint_readings"),
        ("show the photos from b4_back", "evidence"),
        ("were thermal shots taken at c2_back on 18 august", "evidence"),
        ("which annotated images exist for a5_back", "evidence"),
        ("did the pictures at b1_front load properly", "evidence"),
        ("list the images captured in rowB_front", "evidence"),
    ] + OFF_TOPIC,
}

# Hand-written tests on sis_racks names, two per route, used for a hand-written route.
TESTS = {
    "derivation": [
        ("did any checkpoint in rowB_front run above 30 degrees", "threshold_compare"),
        ("which stops broke the vibration limit on 4 august", "threshold_compare"),
        ("how many stops failed on 30 july", "condition_count"),
        ("how many findings are awaiting review", "condition_count"),
        ("what share of checkpoints were completed on 18 august", "proportion"),
        ("how much of the route got done on the latest run", "proportion"),
        ("mean vibration per zone on 19 august", "group_mean"),
        ("average temperature in rowA_back", "group_mean"),
        ("which zone hit the highest temperature on 4 august", "group_max"),
        ("what was the peak vibration in rowC_front", "group_max"),
        ("the five highest temperatures on the latest run", "rank_top_n"),
        ("where did a3_back rank for vibration", "rank_top_n"),
        ("did any checkpoints drop off the route since last time", "run_set_difference"),
        ("is c7_back on both of the last two runs", "run_set_difference"),
        ("when did the first inspection happen", "run_date_range"),
        ("what day had the most runs in october", "run_date_range"),
        ("how many critical alerts did the latest run raise", "sensor_alert_count"),
        ("sensor alerts per run by severity", "sensor_alert_count"),
        ("how many thermal shots were captured on 30 july", "evidence_count"),
        ("how many images are missing on the latest run", "evidence_count"),
        ("does the failed count on the latest run match the checkpoints", "count_check"),
        ("are the declared totals right for 19 august", "count_check"),
        ("what notes are recorded at b4_front", "records"),
        ("show the findings recorded on 30 july", "records"),
    ],
    "lookup": [
        ("which zone comes after rowA_front", "zones"),
        ("what zones did the robot visit on 30 july", "zones"),
        ("was c5_front reached on 30 july", "checkpoints"),
        ("which zone is ups_front in", "checkpoints"),
        ("what was the humidity at b2_back on the latest run", "checkpoint_readings"),
        ("why is a5_front missing a temperature reading", "checkpoint_readings"),
        ("show the thermal pictures from c3_back", "evidence"),
        ("any images captured at a6_back on 4 august", "evidence"),
    ],
}


def build_router(routes: dict[str, list[str]]) -> IndexedRouter:
    return IndexedRouter(
        encoder=FastEmbedEncoder(name=ENCODER),
        routes=[Route(name=name, utterances=utterances) for name, utterances in routes.items()],
    )


def run_tests(router: IndexedRouter, tests: list[tuple[str, str | None]], title: str) -> None:
    print(f"\n{title}")
    print(f"{'question':<62} {'expected':<20} {'got':<20} {'conf':>5}  ok")
    passed = 0
    for question, expected in tests:
        choice = router(normalise(question))
        ok = choice.name == expected
        passed += ok
        confidence = f"{choice.confidence:.2f}" if choice.confidence is not None else "-"
        print(f"{question[:61]:<62} {str(expected):<20} {str(choice.name):<20} {confidence:>5}  {'yes' if ok else 'NO'}")
    print(f"{passed} of {len(tests)} passed")


def calibrate(router: IndexedRouter, calibration: list[tuple[str, str | None]]) -> None:
    questions = [normalise(question) for question, _ in calibration]
    labels = [label for _, label in calibration]
    report = router.fit(X=questions, y=labels)
    print(f"fit: {report.n} questions, accuracy {report.accuracy:.2f}, calibration error {report.ece:.2f}")
    router.confidence_threshold = THRESHOLD


def main() -> None:
    # A branch and a question on the command line: load that calibrated
    # router and route just that one.
    if len(sys.argv) > 1:
        branch = sys.argv[1]
        if branch not in ROUTER_FILES or len(sys.argv) < 3:
            sys.exit("usage: python -m router.second_step_router derivation|lookup \"your question\"")
        if not ROUTER_FILES[branch].exists():
            sys.exit(f"{ROUTER_FILES[branch]} not found: run without a question first to calibrate")
        router = load_router(ROUTER_FILES[branch])
        question = " ".join(sys.argv[2:])
        routed = normalise(question)
        choice = router(routed)
        confidence = f"{choice.confidence:.2f}" if choice.confidence is not None else "-"
        print(f"{question}\n  routed as: {routed}\n  {branch} route: {choice.name}  confidence: {confidence}  "
              f"(threshold {router.confidence_threshold})")
        # Every route, best first, whatever its confidence. Each is the fitted
        # curve applied to that route's own score, so they need not add up to 1.
        print("  every route:")
        for each in router(routed, limit=None, threshold=float("-inf")):
            print(f"    {each.name:<20} {each.confidence:.2f}")
        return

    split = split_question_set()
    for branch, hand_routes in ROUTES.items():
        labelled = [
            (q["second_step"], q["routes"], q["calibration"], q["tests"]) for q in split if q["first_step"] == branch
        ]
        routes, calibration, tests, hand_written = build_sets(
            labelled, hand_routes, CALIBRATION[branch], TESTS[branch],
        )
        print(f"{branch}: {len(labelled)} questions; {sum(map(len, routes.values()))} utterances, "
              f"{len(calibration)} calibration, {len(tests)} tests")
        if hand_written:
            print(f"hand-written, as the question sets give them nothing: {', '.join(hand_written)}")
        router = build_router(routes)
        # Before fit: confidence is the raw similarity, and with no threshold
        # the best match always wins.
        run_tests(router, tests, f"{branch} router before fit (raw scores, no threshold)")
        print()
        calibrate(router, calibration)
        run_tests(router, tests, f"{branch} router after fit (probabilities, threshold {THRESHOLD})")
        router.to_yaml(ROUTER_FILES[branch])
        print(f"\ncalibrated {branch} router saved to {ROUTER_FILES[branch]}\n")


if __name__ == "__main__":
    main()
