"""The acceptance tests from TA-01 to TA-30

Each test renders into its own directory under tests/output/ and leaves both
outputs there, the PDF and the manifest, so a run can be read afterwards rather
than only passed or failed:

    tests/output/<test name>/report_<run_id>.pdf
    tests/output/<test name>/report_<run_id>.manifest.json

    $ xdg-open tests/output/test_ta02/*.pdf

The directory is generated and git-ignored. Nothing is written to the project's
own output/, which belongs to the CLI.
"""

from __future__ import annotations

import collections
import re
import shutil
from dataclasses import fields
from datetime import datetime
from pathlib import Path

import pytest
from pypdf import PdfReader

from PIL import Image

from common.loader import RecordParseError
from common.paths import CONFIG_PATH, load_config, parse_filename, setting
from report.cli import Artifacts, run_pipeline
from report.images import MAX_IMAGE_DIMENSION
from report.render import SENSOR_BLOCKS
from report.manifest import write_manifest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: One directory per test id, each holding a whole run - the record and the
#: evidence tree its paths point at - shaped for that test's input. The run
#: directory sits directly under it, so the directory is also the evidence root:
#: stripping `paths.path_prefix` off a recorded path leaves <run_id>/evidence/...
TEST_DATA = Path(__file__).resolve().parent / "test_data"

def supplied_run(test_id: str) -> tuple[Path, Path]:
    """The record and the evidence root of one test id's run."""
    root = TEST_DATA / test_id
    records = sorted(root.glob("*/records/run.json"))
    assert len(records) == 1, f"{root} should hold exactly one run, found {len(records)}"
    return records[0], root


#: Where the tests write what they render. Under tests/ rather than in the
#: system temporary directory, so the documents a run produced are somewhere
#: worth opening: `xdg-open tests/output/test_ta02/*.pdf`.
OUTPUT_DIR = Path(__file__).resolve().parent / "output"


def fresh_output(name: str) -> Path:
    """An empty directory under tests/output/ for one run's documents.

    A directory per run rather than per test: every output file is named after
    its run_id, so two ids reading the same run would otherwise overwrite each
    other's PDF and leave the last one to win. Emptied as it is asked for, so
    what is in there afterwards is what this run produced and not a file left
    by a render that no longer happens.
    """
    directory = OUTPUT_DIR / name
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    return directory


@pytest.fixture(scope="session")
def rendered():
    """Render one of the supplied runs, once per session.

    Most ids share a run with several others, and rendering is nearly all the
    time a test takes: the same document is what six sensor-trust ids ask six
    different questions of. Session-scoped and cached, so a run is rendered the
    first time an id asks for it and read from memory after that, while each id
    still reports its own pass or failure.
    """
    documents: dict[str, Artifacts] = {}

    def render_once(run: str) -> Artifacts:
        if run not in documents:
            documents[run] = render(*supplied_run(run), fresh_output(run))
        return documents[run]

    return render_once


def render(record: Path, evidence_root: Path, output_dir: Path) -> Artifacts:
    """Run the pipeline and write both outputs, as the CLI does.

    run_pipeline writes the PDF and builds the manifest; writing the manifest is
    main()'s last step and not part of the pipeline, so a test calling
    run_pipeline alone gets a directory holding half of what a real run leaves.
    Both are written here so what a test rendered can be read afterwards.
    """
    artifacts = run_pipeline(record, evidence_root, output_dir)
    write_manifest(artifacts.manifest, output_dir)
    return artifacts


def pdf_pages(path: Path) -> list[str]:
    """The text of each page of a PDF, in order.

    Read back out of the written file rather than off the HTML that produced
    it: the pass conditions are about the document the client opens, and a
    string present in the markup but lost in layout would pass the wrong test.
    """
    return [page.extract_text() for page in PdfReader(path).pages]


def pdf_text(path: Path) -> str:
    """Every page of a PDF as one string."""
    return "\n".join(pdf_pages(path))


def flat(text: str) -> str:
    """Text with every run of whitespace collapsed to one space.

    A PDF has no lines, only glyphs at positions, and extraction puts a break
    wherever the layout did. A reason that fits one line in the checkpoint
    section wraps inside a narrow table cell on the coverage page, so the same
    sentence comes back two ways. Comparing flattened text asks whether the
    words are there, which is what the pass condition is about, rather than
    where the column happened to break them.
    """
    return " ".join(text.split())


def block_between(text: str, start: str, ends: tuple[str, ...]) -> str:
    """The flattened text from `start` up to whichever of `ends` comes first.

    The findings summary is three blocks under three headings, and which block a
    finding landed in is the whole of TA-05. A PDF has no structure to ask, so
    the block is the text between its heading and the next one.
    """
    whole = flat(text)
    assert start in whole, f"{start!r} does not appear in the text"
    opening = whole.index(start)
    closing = min(
        (whole.index(end, opening + len(start)) for end in ends if end in whole[opening:]),
        default=len(whole),
    )
    return whole[opening:closing]


def squashed(text: str) -> str:
    """Text with every space removed.

    For an identifier rather than a sentence. `flat` joins broken lines with a
    space, which is right for prose and wrong for a finding_id: 2.4 warns the
    field is 80 characters of colon-delimited ids, it is set to break rather
    than overflow its cell, and it comes back as "...:general_c ondition" with
    a space inside a word that has none. Removing the spaces asks whether the
    id is there, which is the question, rather than where the cell broke it.
    """
    return "".join(text.split())


def section_text(artifacts: Artifacts, checkpoint_id: str) -> str:
    """The flattened text of one checkpoint's whole section.

    `page_holding` gives the page a heading landed on, which is enough while
    the thing being asked about is near the heading. A checkpoint's sensor
    table is not: with a full evidence grid above it the block runs over two
    pages and the table lands on the second. The section is therefore the text
    from this checkpoint's heading to the next checkpoint's, or to the run
    summary for the last one.

    The running head and footer of any page in between come along with it. They
    say nothing a sensor assertion looks for, so they are left where they are
    rather than filtered out on a guess about what a page carries.
    """
    others = tuple(
        f"{c.checkpoint_id} Sequence"
        for c in artifacts.record.checkpoints
        if c.checkpoint_id != checkpoint_id
    )
    return block_between(
        pdf_text(artifacts.pdf_path),
        f"{checkpoint_id} Sequence",
        others + ("Every checkpoint on the route",),
    )


def page_holding(pages: list[str], marker: str) -> str:
    """The one page carrying `marker`, or a failure naming what was looked for.

    Lets a test say "this page says that" instead of "the document says it
    somewhere", which is the difference between a block rendering in the
    checkpoint it belongs to and rendering anywhere at all.
    """
    found = [page for page in pages if marker in page]
    assert len(found) == 1, f"expected one page carrying {marker!r}, found {len(found)}"
    return found[0]

# --- one check per test id ---------------------------------------------------
#
# Each takes what one rendered run produced and asserts that id's pass
# condition, so the checks read beside the brief's table and the test below can
# run all five and report every one that failed rather than only the first.


def check_ta01(artifacts: Artifacts) -> None:
    """TA-01. The reference run: PDF produced, no errors, all 8 checkpoints present.

    "No errors" is asserted by rendering with nothing caught - any exception on
    the way through fails where it happened, which says more than a caught one
    re-reported here would.
    """
    assert len(artifacts.record.checkpoints) == 8
    assert artifacts.pdf_path.is_file()
    assert artifacts.pdf_path.stat().st_size > 0

    text = pdf_text(artifacts.pdf_path)
    missing = [
        checkpoint.checkpoint_id
        for checkpoint in artifacts.record.checkpoints
        if checkpoint.checkpoint_id not in text
    ]
    assert missing == [], f"not in the PDF: {missing}"
    assert artifacts.manifest["checkpoints_rendered"] == 8


def check_ta02(artifacts: Artifacts) -> None:
    """TA-02. Checkpoint with evidence_images: [] and observed: "no_evidence":
    section renders with an explicit "No evidence captured" block. Not skipped,
    not a crash.
    """
    subjects = [
        c for c in artifacts.record.checkpoints
        if not c.evidence_images and c.observed == "no_evidence"
    ]
    assert subjects, "needs a checkpoint with evidence_images: [] and observed: no_evidence"

    pages = pdf_pages(artifacts.pdf_path)
    for checkpoint in subjects:
        # Not skipped: the checkpoint has a section, and the block is on that
        # page rather than somewhere else in the document.
        section = page_holding(pages, f"{checkpoint.checkpoint_id} Sequence")
        assert "No evidence captured" in section, checkpoint.checkpoint_id
        assert "NO_EVIDENCE" in section, checkpoint.checkpoint_id

    # What a reader sees and what the manifest carries are one fact.
    raised = {
        gap["item_id"] for gap in artifacts.manifest["gaps"]
        if gap["gap_type"] == "NO_EVIDENCE"
    }
    assert {c.checkpoint_id for c in subjects} <= raised


def check_ta03(artifacts: Artifacts) -> None:
    """TA-03. Checkpoint with status: MISSED and a missed_reason: section
    renders, reason printed, counted on the coverage page.
    """
    missed = [c for c in artifacts.record.checkpoints if c.status == "MISSED"]
    assert missed, "needs a checkpoint with status MISSED"
    assert all(c.missed_reason for c in missed), (
        f"needs a missed_reason on each: {[c.checkpoint_id for c in missed if not c.missed_reason]}"
    )

    pages = pdf_pages(artifacts.pdf_path)
    for checkpoint in missed:
        section = page_holding(pages, f"{checkpoint.checkpoint_id} Sequence")
        assert "MISSED" in section, checkpoint.checkpoint_id
        assert flat(checkpoint.missed_reason) in flat(section), checkpoint.checkpoint_id

    # Counted on the coverage page: named among the checkpoints not reached,
    # with the reason beside it.
    not_reached = flat(page_holding(pages, "Checkpoints not reached"))
    for checkpoint in missed:
        assert checkpoint.checkpoint_id in not_reached, checkpoint.checkpoint_id
        assert flat(checkpoint.missed_reason) in not_reached, checkpoint.checkpoint_id

    assert artifacts.manifest["counts"]["computed"]["missed"] == len(missed)
    raised = {
        gap["item_id"] for gap in artifacts.manifest["gaps"]
        if gap["gap_type"] == "MISSED_CHECKPOINT"
    }
    assert raised == {c.checkpoint_id for c in missed}


def check_ta04(artifacts: Artifacts) -> None:
    """TA-04. Checkpoint COMPLETED with result_status: FAIL: both badges
    rendered independently. Not collapsed into one verdict.

    2.2 keeps the two apart - `status` says whether the robot reached the
    checkpoint, `result_status` says what it made of what it found there - and
    COMPLETED with FAIL is the pair that proves it, because a report that
    collapsed them would have to drop one.
    """
    subjects = [
        c for c in artifacts.record.checkpoints
        if c.status == "COMPLETED" and c.result_status == "FAIL"
    ]
    assert subjects, "needs a checkpoint COMPLETED with result_status FAIL"

    pages = pdf_pages(artifacts.pdf_path)
    for checkpoint in subjects:
        section = flat(page_holding(pages, f"{checkpoint.checkpoint_id} Sequence"))
        # Both rendered, and together with nothing between them: one badge
        # beside another rather than one carrying a verdict made of the pair.
        assert "COMPLETED" in section, checkpoint.checkpoint_id
        assert "FAIL" in section, checkpoint.checkpoint_id
        assert "COMPLETED FAIL" in section, checkpoint.checkpoint_id

    # Independently, where it can be counted: the same checkpoint is counted
    # among those reached and among those that failed.
    computed = artifacts.manifest["counts"]["computed"]
    reached = [c for c in artifacts.record.checkpoints if c.status == "COMPLETED"]
    failed = [c for c in artifacts.record.checkpoints if c.result_status == "FAIL"]
    assert computed["completed"] == len(reached)
    assert computed["failed"] == len(failed)
    for checkpoint in subjects:
        assert checkpoint in reached and checkpoint in failed

    # Reaching a checkpoint and failing it is not never reaching it.
    raised = {
        gap["item_id"] for gap in artifacts.manifest["gaps"]
        if gap["gap_type"] == "MISSED_CHECKPOINT"
    }
    assert raised.isdisjoint({c.checkpoint_id for c in subjects})


#: The three headings the findings summary splits its findings under. The third
#: renders only when a record used a status section 2 does not list, so it is
#: one of the markers that can close the second block rather than a certainty.
CONFIRMED_HEADING = "Confirmed findings"
REVIEW_HEADING = "Requires review"
UNRECOGNISED_HEADING = "Findings with an unrecognised status"


def check_ta05(artifacts: Artifacts) -> None:
    """TA-05. Finding with status: abstained: under "Requires review". Not
    shown as confirmed. Not dropped.

    Read off the rendered blocks rather than off finding_groups, because what
    3.6 asks is where the finding appears to a reader. Each block is the text
    between its heading and the next one.
    """
    abstained = [f for f in artifacts.record.findings if f.status == "abstained"]
    assert abstained, "needs a finding with status: abstained"

    text = pdf_text(artifacts.pdf_path)
    review = block_between(text, REVIEW_HEADING, (UNRECOGNISED_HEADING, "Sensor alerts"))
    confirmed = block_between(text, CONFIRMED_HEADING, (REVIEW_HEADING,))

    for finding in abstained:
        identifier = squashed(finding.finding_id)
        # Not dropped, and under "Requires review".
        assert identifier in squashed(text), finding.finding_id
        assert identifier in squashed(review), finding.finding_id
        # Not shown as confirmed.
        assert identifier not in squashed(confirmed), finding.finding_id

    # A confirmed finding is still confirmed: the abstained one was moved, not
    # the block relabelled.
    for finding in artifacts.record.findings:
        if finding.status in ("logged", "acknowledged"):
            assert squashed(finding.finding_id) in squashed(confirmed), finding.finding_id


#: The run TA-06 is inspected at, and how many checkpoints it must carry.
#:
#: The brief specifies 60. Set this to whatever the supplied run holds; nothing
#: else in the test changes.
SCALE_RUN = "TA06"
CHECKPOINTS = 45


def check_ta06(artifacts: Artifacts) -> None:
    """TA-06. A 60-checkpoint run, every page inspected: no table row, heading
    or grid cell split across a page break.

    The pass condition is a person reading every page, and this test does not
    replace that. A PDF is glyphs at positions; nothing in the extracted text
    says where a cell's border fell, so a grid cell sliced down the middle
    reads exactly like an intact one. What is asserted here is the part that
    can be: that a run of this size renders at all, that every checkpoint's
    heading survives whole, that no heading is left stranded from the block it
    heads, and that the manifest describes the document that was written.

    A heading stranded at the foot of a page is the failure this catches, and
    it is the one that actually happened: 5.5's `break-inside: avoid` on
    `.checkpoint-block` pushed blocks whole to the next page and left their
    headings behind (decision 109).
    """
    checkpoints = artifacts.record.checkpoints

    assert len(checkpoints) == CHECKPOINTS, (
        f"TA-06 is inspected at scale: {SCALE_RUN} carries {len(checkpoints)} "
        f"checkpoints, {CHECKPOINTS} wanted"
    )
    assert artifacts.pdf_path.is_file()

    pages = pdf_pages(artifacts.pdf_path)

    for checkpoint in checkpoints:
        # One page, and one only: a heading broken over a break would not match
        # whole, and one repeated would match twice.
        section = flat(page_holding(pages, f"{checkpoint.checkpoint_id} Sequence"))

        # And not stranded: the badges and the first row of the detail table
        # head every block (3.2), so a heading whose page carries neither is a
        # heading that was left behind by the block it belongs to.
        assert checkpoint.status in section, (
            f"{checkpoint.checkpoint_id}: heading page carries no status badge"
        )
        assert "Recorded" in section, (
            f"{checkpoint.checkpoint_id}: heading page carries no detail table"
        )

    # Every checkpoint reached the run summary as well as its own section.
    #
    # Read from the summary's opening line to the end of the document rather
    # than off one page: at this size the table runs over several pages, and a
    # page-level check would call every checkpoint after the first page break
    # missing.
    summary = block_between(pdf_text(artifacts.pdf_path), "Every checkpoint on the route", ())
    absent = [c.checkpoint_id for c in checkpoints if c.checkpoint_id not in summary]
    assert absent == [], f"missing from the run summary: {absent}"

    # 4.4: the page count is measured off the document, not estimated, so the
    # manifest and the PDF agree on how many pages were inspected.
    assert artifacts.manifest["page_count"] == len(pages)


#: 5.6's four facts, as the footer labels each one. The label is part of the
#: assertion: "0.1.0" alone could be anything, "Engine 0.1.0" is the engine.
PROVENANCE_LABELS = {
    "engine_version": "Engine",
    "template_version": "Template",
    "generated_at": "Generated",
    "source_run_id": "Run",
}


def check_ta07(artifacts: Artifacts) -> None:
    """TA-07. Any generated report, metadata and footer inspected: engine
    version, template version, timestamp and source run ID all present.

    5.6 asks for them on every page, not somewhere in the document, because a
    page separated from the rest still has to say what produced it and which
    run it belongs to. So every page is checked rather than the first.

    The manifest carries the same four (4.2), and they are checked against the
    footer rather than only for being there: both come from one
    provenance.stamp() call, so a report whose footer and manifest disagree
    about when it was generated has lost that.
    """
    manifest = artifacts.manifest

    for field in PROVENANCE_LABELS:
        assert manifest.get(field), f"manifest has no {field}"

    # The timestamp is a UTC instant, not free text - 5.6 asks for one, and
    # "Generated recently" would satisfy a check that only looked for the word.
    datetime.strptime(manifest["generated_at"], "%Y-%m-%dT%H:%M:%SZ")

    # The two versions are the config's, not the engine's own idea of itself.
    config = load_config(CONFIG_PATH)
    assert manifest["engine_version"] == setting(config, "provenance", "engine_version")
    assert manifest["template_version"] == setting(config, "provenance", "template_version")
    assert manifest["source_run_id"] == artifacts.record.run_id

    # On every page, each fact behind the label that says what it is. Flattened
    # because the footer wraps at a different word on different pages.
    for number, page in enumerate(pdf_pages(artifacts.pdf_path), 1):
        footer = flat(page)
        for field, label in PROVENANCE_LABELS.items():
            assert f"{label} {manifest[field]}" in footer, (
                f"page {number} footer is missing {label} {manifest[field]}"
            )

    # The branding line the footer opens with (5.7), so the configured text is
    # carried rather than quietly dropped.
    assert flat(setting(config, "branding", "footer_text")) in flat(pdf_text(artifacts.pdf_path))

    # Document metadata: the title names the run, so a file saved out of a
    # reader still says which run it is.
    assert artifacts.record.run_id in (PdfReader(artifacts.pdf_path).metadata or {}).get("/Title", "")


# --- 7.2 sensor trust --------------------------------------------------------
#
# "This block decides the milestone." The one rule under all six: a figure the
# device did not stand behind never reaches the page. Each check finds its own
# subject by the condition the brief states, so it asks its question of
# whatever record supplies it.

#: What closes the zone telemetry section: the checkpoint sections follow it,
#: and the run summary follows those if checkpoints are switched off.
ZONE_SECTION_ENDS = ("Checkpoints", "Every checkpoint on the route")

#: Device flag -> the block it governs and the heading that block prints under,
#: with the heading that follows it in the sensor table.
SENSOR_SUBSYSTEMS = {
    "bme680_ok": ("environment", "Environment", ("Accelerometer",)),
    "adxl345_ok": ("accelerometer", "Accelerometer", ("Particulate",)),
    "sps30_ok": ("particulate", "Particulate", ("Sensor warnings", "Findings")),
}

#: Block key -> its measurement rows, as the sensor table labels them. Taken
#: from the renderer rather than repeated here, so a block that gains a field
#: is still fully checked.
SENSOR_FIELDS = tuple((key, tuple(fields)) for key, _, fields in SENSOR_BLOCKS)


def reading_usable(checkpoint) -> bool:
    """Whether the reading as a whole can be read at all.

    2.8 orders the two questions and detect_gaps follows: a reading that is
    disqualified whole never reaches the per-block question, and raises no
    SUBSYSTEM_OFFLINE beside its SENSOR_UNAVAILABLE (decision 10). So the
    sensor table for such a checkpoint has no blocks in it - not a suppressed
    Particulate block, no Particulate block at all.
    """
    sensor = checkpoint.sensor
    return (
        sensor is not None
        and sensor.ok is not False
        and sensor.status == "connected"
        and sensor.sensor_hub_reachable is not False
    )


def offline_checkpoints(artifacts: Artifacts, flag: str) -> list:
    """Every checkpoint with a usable reading that reported `flag` as false.

    Usable first, because a checkpoint can be both - TA-12's degraded reading
    also carries sps30_ok: false - and the block-level rule says nothing about
    one whose whole reading was thrown out.
    """
    return [
        c for c in artifacts.record.checkpoints
        if reading_usable(c) and c.sensor.raw is not None
        and getattr(c.sensor.raw, flag) is False
    ]


def measure_text(value: float) -> str:
    """A figure as the report would print it, to look for one that should not be there."""
    return f"{value:g}"


def assert_block_suppressed(artifacts: Artifacts, flag: str) -> None:
    """The block `flag` governs prints the offline sentence and no figures.

    Shared by TA-09, TA-10 and TA-11, which are one rule asked of three
    devices: the block names the device that was off, in place of - not beside
    - what the record wrote there.
    """
    key, heading, following = SENSOR_SUBSYSTEMS[flag]
    device = flag.removesuffix("_ok")
    subjects = offline_checkpoints(artifacts, flag)
    assert subjects, f"needs a checkpoint whose sensor reports {flag}: false"

    for checkpoint in subjects:
        block = block_between(section_text(artifacts, checkpoint.checkpoint_id),
                              heading, following)
        where = f"{checkpoint.checkpoint_id} {heading}"
        assert f"Not recorded - {device} offline" in block, f"{where}: {block!r}"
        assert "SUBSYSTEM_OFFLINE" in block, where

        # And no figure survives into it. Asserted by the row labels rather
        # than by the values: a suppressed block renders no measurement rows at
        # all, so no label means no figure, whatever the figure was. Hunting
        # for the values themselves does not work - a recorded 0.0 prints as
        # "0", which is a substring of the word "sps30" in the sentence above.
        for _, label in dict(SENSOR_FIELDS)[key]:
            assert label not in block, (
                f"{where}: rendered the {label} row from an offline device"
            )

    # And named in the manifest. Decision 171 files one run-level gap when a
    # device was off wherever it could be read, so the gap is matched on the
    # device it names rather than on a checkpoint it might not be filed under.
    offline = [
        gap for gap in artifacts.manifest["gaps"]
        if gap["gap_type"] == "SUBSYSTEM_OFFLINE" and gap["detail"].startswith(flag)
    ]
    assert offline, f"no SUBSYSTEM_OFFLINE naming {flag} in the manifest"


def recorded_values(block: object | None) -> list[tuple[str, float]]:
    """Every figure a measurement block actually carries, field and value.

    The schema's dataclasses use slots, so there is no __dict__ to read; the
    field list is the dataclass's own.
    """
    if block is None:
        return []
    return [
        (f.name, getattr(block, f.name))
        for f in fields(block)
        if getattr(block, f.name) is not None
    ]


def check_ta09(artifacts: Artifacts) -> None:
    """TA-09. sps30_ok false and all particulate values 0.0: particulate reads
    "Not recorded - sensor offline", naming the device. A rendered 0.0 fails
    the milestone. SUBSYSTEM_OFFLINE in the manifest.
    """
    assert_block_suppressed(artifacts, "sps30_ok")

    # The trap this test is named for, stated in its own terms: the recorded
    # zeros are what a reader would take for a measurement of clean air.
    for checkpoint in offline_checkpoints(artifacts, "sps30_ok"):
        block = block_between(section_text(artifacts, checkpoint.checkpoint_id),
                              "Particulate", ("Sensor warnings", "Findings"))
        assert "0.0" not in block, f"{checkpoint.checkpoint_id}: particulate printed a zero"


def check_ta10(artifacts: Artifacts) -> None:
    """TA-10. bme680_ok: false with plausible non-zero temperature and humidity
    present: environment block suppressed identically. Plausible values are not
    an exemption.
    """
    # The exemption this test exists to deny: the values are there, and they
    # look like real weather.
    subjects = offline_checkpoints(artifacts, "bme680_ok")
    assert subjects, "needs a checkpoint whose sensor reports bme680_ok: false"
    assert any(
        any(value for _, value in recorded_values(c.sensor.environment))
        for c in subjects
    ), "needs plausible non-zero environment values behind the false flag"

    assert_block_suppressed(artifacts, "bme680_ok")


def check_ta11(artifacts: Artifacts) -> None:
    """TA-11. adxl345_ok: false: accelerometer block suppressed. Zone telemetry
    rollup excludes that checkpoint's vibration figures.
    """
    assert_block_suppressed(artifacts, "adxl345_ok")

    # 3.4's table is the second place a suppressed figure could reach a reader,
    # and the rollup runs over samples rather than over the checkpoint, so the
    # flag has to reach it by way of the zone (decision 164).
    #
    # The section rather than the row: 3.4 draws two tables, and decision 173
    # lets the suppression be stated once beneath them when the device was off
    # for the whole run rather than cell by cell. Both say the same thing, and
    # this asks that the rollup says it somewhere rather than printing figures.
    telemetry = block_between(
        pdf_text(artifacts.pdf_path), "Every zone the run passed through", ZONE_SECTION_ENDS,
    )
    assert "adxl345 offline" in telemetry, (
        "the zone telemetry rollup does not say the accelerometer was offline"
    )


def check_ta12(artifacts: Artifacts) -> None:
    """TA-12. sensor.ok: false and status: "degraded": whole sensor table reads
    "Sensor unavailable". SENSOR_UNAVAILABLE in the manifest.
    """
    subjects = [
        c for c in artifacts.record.checkpoints
        if c.sensor is not None and (c.sensor.ok is False or c.sensor.status != "connected")
    ]
    assert subjects, "needs a checkpoint with sensor.ok: false and status: degraded"

    unavailable = {
        gap["item_id"] for gap in artifacts.manifest["gaps"]
        if gap["gap_type"] == "SENSOR_UNAVAILABLE"
    }
    for checkpoint in subjects:
        section = section_text(artifacts, checkpoint.checkpoint_id)
        assert "Sensor unavailable" in section, checkpoint.checkpoint_id
        assert "SENSOR_UNAVAILABLE" in section, checkpoint.checkpoint_id
        assert checkpoint.checkpoint_id in unavailable, checkpoint.checkpoint_id

        # The whole reading is disqualified, so the per-block question is never
        # reached: no block heading, and no per-block gap beside it (2.8,
        # decision 10).
        assert "Not recorded - " not in section, (
            f"{checkpoint.checkpoint_id}: a block was judged inside an unavailable reading"
        )


def check_ta13(artifacts: Artifacts) -> None:
    """TA-13. age_seconds: 480: values render, flagged stale with the age.
    STALE_READING in the manifest.

    The one case where a figure still prints. The reading is old, not wrong,
    and 2.8 asks for it to be shown with its age rather than withheld.
    """
    limit = setting(load_config(CONFIG_PATH), "sensor", "max_age_seconds")

    subjects = [
        c for c in artifacts.record.checkpoints
        if reading_usable(c) and (c.sensor.age_seconds or 0) > limit
    ]
    assert subjects, f"needs a checkpoint with a usable reading older than {limit} s"

    stale = {
        gap["item_id"] for gap in artifacts.manifest["gaps"]
        if gap["gap_type"] == "STALE_READING"
    }
    for checkpoint in subjects:
        section = section_text(artifacts, checkpoint.checkpoint_id)
        assert "Reading stale" in section, checkpoint.checkpoint_id
        assert "STALE_READING" in section, checkpoint.checkpoint_id
        # Flagged with the age, not merely flagged.
        assert measure_text(checkpoint.sensor.age_seconds) in section, checkpoint.checkpoint_id
        assert checkpoint.checkpoint_id in stale, checkpoint.checkpoint_id

        # Values render: a stale reading is still a reading.
        environment = checkpoint.sensor.environment
        if environment is not None and environment.temperature_c is not None:
            block = block_between(section, "Environment", ("Accelerometer",))
            assert measure_text(environment.temperature_c) in block, (
                f"{checkpoint.checkpoint_id}: a stale reading was withheld rather than flagged"
            )


def check_ta14(artifacts: Artifacts) -> None:
    """TA-14. Checkpoint with no sensor key at all: handled as unavailable, not
    a crash.
    """
    subjects = [c for c in artifacts.record.checkpoints if c.sensor is None]
    assert subjects, "needs a checkpoint with no sensor key at all"

    unavailable = {
        gap["item_id"] for gap in artifacts.manifest["gaps"]
        if gap["gap_type"] == "SENSOR_UNAVAILABLE"
    }
    for checkpoint in subjects:
        section = section_text(artifacts, checkpoint.checkpoint_id)
        assert "Sensor unavailable" in section, checkpoint.checkpoint_id
        assert checkpoint.checkpoint_id in unavailable, checkpoint.checkpoint_id
        # Nothing invented to fill the absence.
        assert "Not recorded - " not in section, checkpoint.checkpoint_id


# --- 7.3 evidence images -----------------------------------------------------
#
# Four of the eight are one rule in four forms: the report says what evidence
# it does not have, and says which kind of not-having it was. The other four
# are about what it does with the evidence it has.


def views_of(checkpoint) -> tuple[list[str], list[str]]:
    """The view labels a checkpoint recorded, RGB and thermal, in recorded order."""
    rgb, thermal = [], []
    for uri in checkpoint.evidence_images:
        view, is_thermal = parse_filename(uri, checkpoint.checkpoint_id)
        (thermal if is_thermal else rgb).append(view)
    return rgb, thermal


def grid_views(grid: str) -> list[str]:
    """The view labels a rendered grid carries, in the order it drew them.

    A cell is a label then its RGB then "Thermal" then its thermal, so the
    labels are the tokens immediately before each "Thermal". Read this way
    rather than by searching for each label in turn: "N" is a substring of
    "NE", of "NW", and of half the words in the running head that shares the
    page, so a positional search finds the wrong "N" every time.
    """
    return re.findall(r"(\S+) Thermal", grid)


def resolved_by_reason(artifacts: Artifacts, fragment: str) -> list[tuple[str, object]]:
    """Every resolved image whose failure reason carries `fragment`, with its checkpoint."""
    return [
        (checkpoint_id, image)
        for checkpoint_id, images in artifacts.images.items()
        for image in images
        if image.reason is not None and fragment in image.reason
    ]


def image_gap_details(artifacts: Artifacts, checkpoint_id: str) -> list[str]:
    """The MISSING_IMAGE details raised against one checkpoint."""
    return [
        gap["detail"] for gap in artifacts.manifest["gaps"]
        if gap["gap_type"] == "MISSING_IMAGE" and gap["item_id"] == checkpoint_id
    ]


def check_ta15(artifacts: Artifacts) -> None:
    """TA-15. Checkpoint with all 8 views and all 8 thermals: 16 images
    rendered, thermal beneath its RGB pair, grid in order.

    The brief says "fixed compass order". Decision 174 took the compass out of
    the grid, so the order asserted here is the order the record listed the
    views in - which for a route that records compass points is compass order,
    and is the only order there is for a route that does not.
    """
    subjects = [
        c for c in artifacts.record.checkpoints
        if len(views_of(c)[0]) == 8 and len(views_of(c)[1]) == 8
    ]
    assert subjects, "needs a checkpoint with 8 RGB and 8 thermal images"

    for checkpoint in subjects:
        rgb, thermal = views_of(checkpoint)
        assert sorted(rgb) == sorted(thermal), f"{checkpoint.checkpoint_id}: unpaired views"

        section = section_text(artifacts, checkpoint.checkpoint_id)
        grid = block_between(section, "Evidence - ", ("Sensor",))

        # Every view labelled, in the order the record listed them, and one
        # "Thermal" label under each: eight cells, sixteen pictures.
        assert grid_views(grid) == rgb, (
            f"{checkpoint.checkpoint_id}: grid drew {grid_views(grid)}, record listed {rgb}"
        )

        # No cell saying a picture is absent.
        assert "Image not captured" not in grid, checkpoint.checkpoint_id
        assert "No thermal" not in grid, checkpoint.checkpoint_id

        # 16 images rendered, counted where the manifest counts them.
        assert len(checkpoint.evidence_images) == 16
        assert all(i.readable for i in artifacts.images[checkpoint.checkpoint_id])


def check_ta16(artifacts: Artifacts) -> None:
    """TA-16. Checkpoint with 8 RGB and 0 thermal: cells render, the thermal
    half says so, MISSING_THERMAL names the views.

    The brief says eight cells each reading "No thermal". The engine says it
    once beneath the grid instead: eight cells repeating one sentence is the
    same fact printed eight times, and decision 150 collapses only the case
    where every cell would say the same thing. The cells keep their RGB either
    way, and the gap still names every view.
    """
    subjects = [
        c for c in artifacts.record.checkpoints
        if len(views_of(c)[0]) == 8 and not views_of(c)[1]
    ]
    assert subjects, "needs a checkpoint with 8 RGB and no thermal"

    for checkpoint in subjects:
        rgb, _ = views_of(checkpoint)
        section = section_text(artifacts, checkpoint.checkpoint_id)

        # Said once, and said as an absence rather than left blank.
        assert "No thermal image was captured for any view" in section, checkpoint.checkpoint_id
        assert "MISSING_THERMAL" in section, checkpoint.checkpoint_id

        # And naming the views, in the manifest.
        detail = next(
            gap["detail"] for gap in artifacts.manifest["gaps"]
            if gap["gap_type"] == "MISSING_THERMAL"
            and gap["item_id"] == checkpoint.checkpoint_id
        )
        for view in rgb:
            assert view in detail, f"{checkpoint.checkpoint_id}: {view} not named in {detail!r}"


def check_ta17(artifacts: Artifacts) -> None:
    """TA-17. Checkpoint with fewer views than the rest: it still renders, and
    what it did record is all there.

    The brief asks for eight cells here, three of them "Image not captured".
    Decision 174 removed the fixed eight-cell grid: with no closed vocabulary
    of views there is nothing that says a view was meant to exist, so a
    checkpoint gets one cell per view it recorded and no placeholders for views
    nobody defined. What survives of TA-17 is that the checkpoint is not
    skipped and nothing it recorded is dropped, which is what this asserts.

    This is a known deviation, not an oversight. It is recorded in decision 174
    and needs settling with the client before delivery.
    """
    counts = {len(views_of(c)[0]) for c in artifacts.record.checkpoints if c.evidence_images}
    assert len(counts) > 1, "needs a checkpoint recording fewer views than another"

    fewest = min(counts)
    subjects = [
        c for c in artifacts.record.checkpoints
        if c.evidence_images and len(views_of(c)[0]) == fewest
    ]

    for checkpoint in subjects:
        rgb, _ = views_of(checkpoint)
        grid = block_between(section_text(artifacts, checkpoint.checkpoint_id),
                             "Evidence - ", ("Sensor",))
        for view in rgb:
            assert view is None or view in grid, (
                f"{checkpoint.checkpoint_id}: recorded view {view} is not in the grid"
            )


def check_ta18(artifacts: Artifacts) -> None:
    """TA-18. A referenced path that does not exist under the evidence root:
    cell shows "Image not available". MISSING_IMAGE with the reason.
    """
    missing = resolved_by_reason(artifacts, "no file at")
    assert missing, "needs a recorded evidence path that does not resolve"

    for checkpoint_id, image in missing:
        section = section_text(artifacts, checkpoint_id)
        # "Image not available" for an RGB, "Thermal not available" for its
        # counterpart: the same absence, named for the half it happened in.
        assert "not available" in section, checkpoint_id
        assert "MISSING_IMAGE" in section, checkpoint_id

        details = image_gap_details(artifacts, checkpoint_id)
        assert any("no file at" in d for d in details), (
            f"{checkpoint_id}: no MISSING_IMAGE naming the path that did not resolve"
        )


def check_ta19(artifacts: Artifacts) -> None:
    """TA-19. Zero-byte image file: gap, not a crash. detail says the file was empty."""
    empty = resolved_by_reason(artifacts, "file is empty")
    assert empty, "needs a zero-byte image file"

    for checkpoint_id, image in empty:
        details = image_gap_details(artifacts, checkpoint_id)
        assert any("empty" in d for d in details), (
            f"{checkpoint_id}: no MISSING_IMAGE saying the file was empty"
        )
        # The cell carries the reason, not just the fact.
        section = section_text(artifacts, checkpoint_id)
        assert "not available - file is empty" in section, checkpoint_id


def check_ta20(artifacts: Artifacts) -> None:
    """TA-20. A text file renamed .jpg: gap, not a crash. detail distinguishes
    it from the zero-byte case.

    The two reach the page the same way and a reader who has to act on one
    cannot act on "the image is broken": a file that arrived empty was never
    written, one that will not decode was written wrongly.
    """
    undecodable = resolved_by_reason(artifacts, "not a readable image")
    assert undecodable, "needs a file that is not a readable image"

    for checkpoint_id, image in undecodable:
        details = image_gap_details(artifacts, checkpoint_id)
        readable = [d for d in details if "not a readable image" in d]
        assert readable, f"{checkpoint_id}: no MISSING_IMAGE saying the file would not decode"
        # Distinguished: this reason is not the empty-file one, and the cell
        # says which it was rather than only that something is wrong.
        assert not any("empty" in d for d in readable), (
            f"{checkpoint_id}: the two failure kinds share a detail"
        )
        assert "not available - file is not a readable image" in section_text(
            artifacts, checkpoint_id
        ), checkpoint_id


def check_ta21(artifacts: Artifacts) -> None:
    """TA-21. Evidence path containing spaces and mixed case in directory
    names: resolves correctly.

    Asked of the paths that resolved rather than of every path that has a
    space in it. Every path in this run has spaces - the route directory is
    called "SIS Facility Checkpoint Route" - including the ones TA-18 broke on
    purpose, so "every spaced path resolves" would be asking this test to pass
    only when TA-18's inputs are absent. What TA-21 wants is that spaces and
    case are not themselves what stops a path resolving, and that is what a
    resolving path carrying both demonstrates.

    Nothing is case-folded on the way (decision 12), so the local path the
    engine opened carries the same spaces and the same capitals the record
    wrote - checked here, because a resolver that lowercased and then found the
    file anyway would pass a weaker test.
    """
    resolved = [
        image for images in artifacts.images.values() for image in images if image.readable
    ]
    assert resolved, "needs at least one evidence path that resolves"

    spaced = [i for i in resolved if " " in i.original_uri]
    assert spaced, "needs a path with spaces in a directory name that resolves"

    mixed = [
        i for i in spaced
        if any(c.isupper() for c in i.original_uri)
        and any(c.islower() for c in i.original_uri)
    ]
    assert mixed, "needs a path with mixed case in a directory name that resolves"

    # Carried through rather than normalised: what was opened is what was
    # recorded, spaces and capitals included.
    for image in mixed:
        recorded = image.original_uri.rsplit("/", 2)[-2]
        assert recorded in str(image.local_path), (
            f"{image.original_uri}: opened {image.local_path}, not the recorded spelling"
        )


def check_ta22(artifacts: Artifacts) -> None:
    """TA-22. An image larger than the configured maximum: resized before
    embedding, output size within budget.

    Read off the PDF rather than off the resize helper: the budget is blown by
    what was embedded, and an image sized down into a temporary file and then
    embedded from the original would pass a test of the helper alone.
    """
    oversized = [
        (checkpoint_id, image)
        for checkpoint_id, images in artifacts.images.items()
        for image in images
        if image.readable and image.local_path is not None
        and max(Image.open(image.local_path).size) > MAX_IMAGE_DIMENSION
    ]
    assert oversized, f"needs a source image longer than {MAX_IMAGE_DIMENSION} px on an edge"

    too_big = set()
    for page in PdfReader(artifacts.pdf_path).pages:
        for embedded in page.images:
            if max(embedded.image.size) > MAX_IMAGE_DIMENSION:
                too_big.add(embedded.image.size)
    assert not too_big, f"embedded at more than {MAX_IMAGE_DIMENSION} px: {sorted(too_big)}"


# --- 7.4 reconciliation and malformed input ----------------------------------
#
# Six of the eight share a run: a record can carry every contradiction at once
# without any of them standing in the way of another. Two cannot join it. TA-26
# is a file that does not parse, so there is nothing to put the others in, and
# TA-30 has no checkpoints, which is the one thing the other five need.


def declared_and_computed(artifacts: Artifacts, row: str) -> tuple[object, object]:
    """One reconciliation row's declared and computed figures."""
    counts = artifacts.manifest["counts"]
    return counts["declared"][row], counts["computed"][row]


def count_mismatch_details(artifacts: Artifacts) -> list[str]:
    """Every COUNT_MISMATCH detail the run raised."""
    return [
        gap["detail"] for gap in artifacts.manifest["gaps"]
        if gap["gap_type"] == "COUNT_MISMATCH"
    ]


def coverage_table(artifacts: Artifacts) -> str:
    """The reconciliation table, declared against computed."""
    return block_between(
        pdf_text(artifacts.pdf_path), "Row Declared Computed", ("Checkpoints not reached",),
    )


def check_ta23(artifacts: Artifacts) -> None:
    """TA-23. warned_checkpoints: 0 while checkpoints carry sensor warnings:
    coverage page prints declared and computed side by side and flags the
    conflict. COUNT_MISMATCH in the manifest.

    2.9 calls this out by name: "warned_checkpoints is 0 while six of eight
    checkpoints carry a non-empty sensor.warnings array... That contradiction
    must appear in the report."
    """
    warned = [c for c in artifacts.record.checkpoints if c.sensor and c.sensor.warnings]
    assert warned, "needs checkpoints carrying sensor warnings"
    assert artifacts.record.warned_checkpoints == 0, "needs warned_checkpoints declared as 0"

    assert "Warned" in coverage_table(artifacts)

    details = " ".join(count_mismatch_details(artifacts))
    assert "warned" in details.lower(), (
        f"{len(warned)} checkpoints carry sensor warnings and warned_checkpoints is 0, "
        "but no COUNT_MISMATCH names it"
    )


def check_ta24(artifacts: Artifacts) -> None:
    """TA-24. finding_count: 3 with two entries in findings: both figures
    printed, conflict flagged. Neither silently preferred.
    """
    declared, computed = declared_and_computed(artifacts, "findings")
    assert declared != computed, "needs finding_count to disagree with the findings array"
    assert computed == len(artifacts.record.findings)

    # Both printed, side by side, neither replaced by the other. The row
    # carries an asterisk between its label and its figures when the two
    # disagree, so the figures are read out of the row rather than matched
    # against a fixed string.
    row = block_between(coverage_table(artifacts), "Findings", ("*  Declared", "Count conflicts"))
    figures = re.findall(r"-?\d+", row)
    assert figures[:2] == [str(declared), str(computed)], (
        f"the Findings row reads {row!r}, expected {declared} beside {computed}"
    )

    details = " ".join(count_mismatch_details(artifacts))
    assert "finding_count" in details, details


def check_ta25(artifacts: Artifacts) -> None:
    """TA-25. An event_log evidence_count that disagrees with the checkpoint's
    evidence_images: the discrepancy is surfaced per checkpoint.
    """
    claims = {
        event.checkpoint_id: event.evidence_count
        for event in artifacts.record.event_log
        if event.checkpoint_id and event.evidence_count is not None
    }
    disagreeing = [
        c for c in artifacts.record.checkpoints
        if c.checkpoint_id in claims and claims[c.checkpoint_id] != len(c.evidence_images)
    ]
    assert disagreeing, "needs an event whose evidence_count disagrees with the checkpoint"

    for checkpoint in disagreeing:
        details = [
            gap["detail"] for gap in artifacts.manifest["gaps"]
            if gap["gap_type"] == "COUNT_MISMATCH" and gap["item_id"] == checkpoint.checkpoint_id
        ]
        assert details, f"{checkpoint.checkpoint_id}: no COUNT_MISMATCH against this checkpoint"
        both = " ".join(details)
        assert str(claims[checkpoint.checkpoint_id]) in both, both
        assert str(len(checkpoint.evidence_images)) in both, both


def check_ta26(record: Path, evidence_root: Path, output_dir: Path) -> None:
    """TA-26. Malformed JSON, a syntax error: a clear error naming the file and
    the parse failure. Not a stack trace, not a silent empty report.

    Takes the paths rather than an Artifacts, because the point is that there
    is no Artifacts: the run stops at the loader.
    """
    try:
        render(record, evidence_root, output_dir)
    except RecordParseError as error:
        message = str(error)
    else:
        raise AssertionError("a record that is not JSON was loaded without complaint")

    assert record.name in message, message
    assert "not valid JSON" in message, message
    assert "line" in message and "column" in message, message
    assert not list(output_dir.glob("*.pdf")), "a PDF was written for an unparseable record"


def check_ta27(artifacts: Artifacts) -> None:
    """TA-27. confidence present as the string "0.94": handled or reported
    clearly. Never silently coerced into a number that then appears as a
    measurement.
    """
    refused = [
        a for a in artifacts.record.anomalies
        if a.field_name == "confidence" and a.kind == "datatype"
    ]
    assert refused, "needs a confidence recorded as a string"

    for anomaly in refused:
        checkpoint = next(
            c for c in artifacts.record.checkpoints if c.checkpoint_id == anomaly.item_id
        )
        assert checkpoint.confidence is None, anomaly.item_id

        section = section_text(artifacts, anomaly.item_id)
        assert "INCORRECT_DATATYPE" in section, anomaly.item_id
        assert "0.94" in section, anomaly.item_id

        datatype = {
            gap["item_id"] for gap in artifacts.manifest["gaps"]
            if gap["gap_type"] == "INCORRECT_DATATYPE"
        }
        assert anomaly.item_id in datatype, anomaly.item_id


def check_ta28(artifacts: Artifacts) -> None:
    """TA-28. Duplicate checkpoint_id values in one run: detected and reported.
    Not silently deduplicated.
    """
    seen = collections.Counter(c.checkpoint_id for c in artifacts.record.checkpoints)
    duplicated = {cid for cid, n in seen.items() if n > 1}
    assert duplicated, "needs a checkpoint_id used more than once"

    for checkpoint_id in duplicated:
        assert seen[checkpoint_id] > 1
    assert artifacts.manifest["checkpoints_rendered"] == len(artifacts.record.checkpoints)

    reported = [
        a for a in artifacts.record.anomalies
        if a.kind == "duplicate_id" and a.item_id in duplicated
    ]
    assert reported, f"no duplicate_id anomaly for {duplicated}"


def check_ta29(artifacts: Artifacts) -> None:
    """TA-29. Unicode and special characters in notes and description,
    including <script>alert(1)</script> and {{ config }}: renders as literal
    text. No broken glyphs, no execution, no interpolation.
    """
    hostile = [
        (c.checkpoint_id, c.notes) for c in artifacts.record.checkpoints
        if c.notes and "<script>" in c.notes
    ]
    assert hostile, "needs notes carrying markup and template syntax"

    for checkpoint_id, notes in hostile:
        section = flat(section_text(artifacts, checkpoint_id))

        # As written: the markup is text, not a tag that vanished into the
        # document, and the template syntax is text, not a value.
        assert "<script>alert(1)</script>" in section, checkpoint_id
        assert "{{ config }}" in section, checkpoint_id

        # Not interpolated: nothing the config holds leaked in where the
        # template expression was.
        assert "engine_version" not in section, checkpoint_id

        # Unicode survives rather than arriving as a replacement glyph.
        for character in "\u00b0\u00e9\u2014\u2713":
            if character in notes:
                assert character in section, f"{checkpoint_id}: lost {character!r}"
        assert "\ufffd" not in section, f"{checkpoint_id}: a character did not render"


def check_ta30(artifacts: Artifacts) -> None:
    """TA-30. checkpoints: [], valid structure otherwise: cover, coverage page,
    a plain "no checkpoints recorded" statement. EMPTY_RECORD. Not a crash.
    """
    assert artifacts.record.checkpoints == (), "needs a record with no checkpoints"
    assert artifacts.pdf_path.is_file(), "no PDF was produced for an empty record"

    text = flat(pdf_text(artifacts.pdf_path))

    assert artifacts.record.run_id in text
    assert "Coverage and reconciliation" in text

    # Said plainly, and said in the section a reader goes to for checkpoints.
    # Asserted on that section rather than on the document, because the run
    # summary has an absence line of its own and would satisfy a check that
    # only asked whether the words appear anywhere (decision 177).
    assert "No checkpoints were recorded for this run" in text, text[:400]
    section = page_holding(pdf_pages(artifacts.pdf_path), "no checkpoint sections to show")
    assert "EMPTY_RECORD" in section, section[:300]

    empty = [g for g in artifacts.manifest["gaps"] if g["gap_type"] == "EMPTY_RECORD"]
    assert empty, "no EMPTY_RECORD gap"
    assert empty[0]["item_id"] == "__run__", empty


#: Section 7's thirty test ids: the run each is asked of, and the check it is.
#:
#: The run is the fixture directory under tests/test_data/. Ids share one where
#: their inputs do not conflict, which is most of them - a record can carry a
#: checkpoint the robot never reached, one whose sensor hub was degraded and one
#: with a confidence recorded as a string, all at once.
#:
#: Two ids have no run. TA-26's file does not parse, so there is no document to
#: ask anything of - that is the test. TA-08 is "fresh Linux container,
#: following your README only", which is a thing to do rather than a thing to
#: assert, and is listed here so the suite accounts for all thirty rather than
#: quietly covering twenty-nine.
CORE_RUN = "TA01-TA05"
SENSOR_RUN = "TA09-TA14"
EVIDENCE_RUN = "TA15-TA22"
CONFLICT_RUN = "TA23-TA29"
MALFORMED_RUN = "TA26"
EMPTY_RUN = "TA30"

ACCEPTANCE = (
    ("TA-01", CORE_RUN, check_ta01),
    ("TA-02", CORE_RUN, check_ta02),
    ("TA-03", CORE_RUN, check_ta03),
    ("TA-04", CORE_RUN, check_ta04),
    ("TA-05", CORE_RUN, check_ta05),
    ("TA-06", SCALE_RUN, check_ta06),
    ("TA-07", "TA07", check_ta07),
    ("TA-08", None, None),
    ("TA-09", SENSOR_RUN, check_ta09),
    ("TA-10", SENSOR_RUN, check_ta10),
    ("TA-11", SENSOR_RUN, check_ta11),
    ("TA-12", SENSOR_RUN, check_ta12),
    ("TA-13", SENSOR_RUN, check_ta13),
    ("TA-14", SENSOR_RUN, check_ta14),
    ("TA-15", EVIDENCE_RUN, check_ta15),
    ("TA-16", EVIDENCE_RUN, check_ta16),
    ("TA-17", EVIDENCE_RUN, check_ta17),
    ("TA-18", EVIDENCE_RUN, check_ta18),
    ("TA-19", EVIDENCE_RUN, check_ta19),
    ("TA-20", EVIDENCE_RUN, check_ta20),
    ("TA-21", EVIDENCE_RUN, check_ta21),
    ("TA-22", EVIDENCE_RUN, check_ta22),
    ("TA-23", CONFLICT_RUN, check_ta23),
    ("TA-24", CONFLICT_RUN, check_ta24),
    ("TA-25", CONFLICT_RUN, check_ta25),
    ("TA-26", None, check_ta26),
    ("TA-27", CONFLICT_RUN, check_ta27),
    ("TA-28", CONFLICT_RUN, check_ta28),
    ("TA-29", CONFLICT_RUN, check_ta29),
    ("TA-30", EMPTY_RUN, check_ta30),
)


@pytest.mark.parametrize(
    ("test_id", "run", "check"), ACCEPTANCE, ids=[test_id for test_id, _, _ in ACCEPTANCE],
)
def test_acceptance(test_id: str, run: str | None, check, rendered) -> None:
    """One test id of section 7, against the run it is asked of.

    One result per id, so `pytest -v` reads as the results table deliverable 10
    asks for and a failure names the id that failed rather than the block it
    was in.
    """
    if check is None:
        pytest.skip("run by hand: a fresh container following the README only")

    if run is None:
        # TA-26. There is nothing to render, which is the whole of the test.
        check(*supplied_run(MALFORMED_RUN), fresh_output(MALFORMED_RUN))
        return

    check(rendered(run))
