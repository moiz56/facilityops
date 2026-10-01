# The analytical agent (B-1)

This folder holds the agent that answers questions about facility inspection
runs. It answers two kinds of question:

- **Figures worked out over the runs**: "which checkpoints went over the
  temperature limit in the latest run?", "what was the mean vibration in
  rowA_back?", "on which days were runs taken?"
- **Values looked up as recorded**: "what was the temperature at a3_back?",
  "why is there no PM2.5 reading there?", "which images were taken at
  a3_back?", "which zones did the route cover?"

You ask in plain English; it answers in plain English, and every number in the
answer comes with a citation that points at the exact place in the records it
came from. When the answer lists evidence images, the answer PDF shows them.

The one rule everything here is built around: **the language model never
writes a number.** It reads the question, points at where the answer is, and
writes a sentence of introduction. Code finds the values, writes them into the
answer, and checks the whole answer before it is shown. If any value cannot be
traced back to the records, the answer is refused rather than shown.

---

## Contents

1. [The data it works on](#1-the-data-it-works-on)
2. [The big picture](#2-the-big-picture)
3. [Step by step: from records to the databases](#3-step-by-step-from-records-to-the-databases)
4. [The eight derivations](#4-the-eight-derivations)
5. [How a question is answered](#5-how-a-question-is-answered)
6. [What can come back](#6-what-can-come-back)
7. [Running it](#7-running-it)
8. [Configuration](#8-configuration)
9. [Testing](#9-testing)
10. [Where things live](#10-where-things-live)
11. [Changing things safely](#11-changing-things-safely)

---

## 1. The data it works on

An inspection robot drives a **route** around a facility. Each time it drives
the route is a **run**, and each run is saved as a `run.json` record.

The data is one hierarchy, each level inside the one above:

```
run                     one drive of the route
└─ zone                 an area of the route, e.g. a row of racks
   └─ checkpoint        one stop in that zone, e.g. a single rack
      ├─ status         whether the robot got there: COMPLETED or MISSED
      ├─ result_status  the verdict: PASS, FAIL or WARN
      ├─ readings       by sensor block, then field (environment → temperature_c)
      └─ evidence       the images captured there
```

A zone holds one checkpoint or several; when it holds one, the two often share
a name. Each run has its own zones and checkpoints, so the same checkpoint has
different values in different runs.

During a run the robot records:

| What | Where it comes from | Example |
|---|---|---|
| Each checkpoint visit | one reading per stop, whether it was completed and whether it passed, and its images | `a3_back`: COMPLETED, PASS, 30.64 °C |
| Telemetry samples | a reading every couple of seconds while driving, tagged with the zone | `sample_0042` in `rowA_back` |
| Findings | things the robot noticed | a blocked aisle, severity warning |
| Sensor alerts | threshold breaches logged during the run | high particulate |
| The event log | run started, checkpoint completed, run completed | |

The sensors are an accelerometer (vibration), an environment sensor
(temperature, humidity, pressure) and a particulate sensor (PM1.0 to PM10).

Several runs are loaded at once, oldest first. Run ids start with when the run
began, e.g. `20260819_114053-sis_racks_checkpoint_route`.

**Missed is not failed.** A MISSED checkpoint was never reached, so it was
never inspected: it has no verdict, and it did not fail. A FAILED checkpoint
was reached, inspected, and its verdict was FAIL. The agent keeps the two
apart everywhere.

---

## 2. The big picture

```
  run.json files
        │
        ▼
  ┌──────────────┐   Load every run, oldest first.
  │  data_agent  │
  └──────────────┘
        │
        ▼
  ┌──────────────┐   Decide which readings can be trusted (eligibility),
  │  derivation  │   then work out the eight kinds of figure for every run.
  └──────────────┘
        │                                   │
        ▼                                   ▼
  EXTENDED RECORD                     ELIGIBILITY VIEW
  (records + figures)                 (run → zone → checkpoint → readings,
        │                              kept or left out, and evidence)
        ▼                                   ▼
  ┌──────────────────┐              ┌──────────────────┐
  │ derivation DB    │              │ lookup DB        │   two separate SQLite
  └──────────────────┘              └──────────────────┘   databases; every row
        │                                   │               remembers its path
        │            a question             │
        │                │                  │
        │     ┌──────────▼───────────┐      │
        │     │ retrieval router     │ (model) derivation, lookup or abstain?
        │     └──────────┬───────────┘      │
        │        ┌───────┴────────┐         │
        ▼        ▼                ▼         ▼
  ┌───────────────────┐    ┌───────────────────┐
  │ derivation router │    │ lookup router     │   (model) which runs, zones,
  │ SQL writer per    │    │ SQL writer per    │   checkpoints, and which
  │ derivation type   │    │ lookup table      │   derivations or tables
  └─────────┬─────────┘    └─────────┬─────────┘
            └──────────┬─────────────┘
                       ▼
     guard (code) → facts, values traced (code) → slot filling (code)
     → prose writer (model, no values) → verification (code)
                       │
                       ▼
     an answer, its citations, its verification, and a PDF (with images)
```

Four model calls on either path: the retrieval router, the router, the SQL
writer (one per derivation type or table), and the prose writer. Code runs
between and after every one of them. The model only ever points or writes
words; code does everything that touches a value.

---

## 3. Step by step: from records to the databases

### 3.1 Loading the runs (`data_agent.py`)

Every record under the data directory (`data_agent/` by default) is parsed by
`common/loader.py` and sorted oldest first by start time. A run with no start
time sorts last rather than being dropped.

### 3.2 Which readings can be trusted (`derivation.py`)

Before anything is worked out, every reading goes through the **exclusion
rule** (eligibility). A reading is left out when:

1. the checkpoint was not completed (e.g. MISSED),
2. the sensor was offline or not connected,
3. the sensor's own health flag said it was faulty (e.g. `sps30_ok=false` for
   the particulate sensor),
4. the value itself is missing or not a number.

Telemetry samples have no health flags of their own, so a sample is judged by
its nearest checkpoint.

Readings left out are never used, never shown as values and never replaced
with a zero. They are kept with their reason instead, so an answer can say why
a reading is missing. If nothing is left to work with, a figure is marked
**NOT_COMPUTABLE** with its reason.

### 3.3 The extended record (`derivation.py`)

The eight derivations (next section) run over the trusted readings. Their
results, together with the untouched records, form the **extended record**:

```
extended record
├── records           the runs, exactly as loaded
└── derived
    ├── values        each derivation's figures, by name (one entry per run for D1-D6)
    ├── excluded      per run: what was left out, where, and why
    └── stale         readings that were used but were older than the limit
```

`python -m agents.main --all` writes it to `output/extended_record.json`.
This is the only place in the agent where arithmetic happens.

### 3.4 The derivation database (`database_derivation.py`)

The extended record is copied into an in-memory SQLite database so the model
can ask for exactly the rows it needs. Nothing is computed here: every value is
copied, and every row carries a `path` column saying where in the extended
record that value lives, e.g. `derived.values.threshold_compare[3].checkpoints.a3_back`.
That path is what later becomes a citation.

| Tables | Holds |
|---|---|
| `runs` | one row per run: status, times, the counts the robot reported |
| `rec_*` | the records as written: checkpoint visits, findings, alerts, events, trusted readings, exclusions |
| `d1_*` … `d8_*` | each derivation's figures, one table (or a few) per derivation |

A copy is saved to `output/b1_database.sqlite` with a fingerprint of
everything it was built from. On the next run, if nothing changed, the copy is
reused instead of rebuilt.

### 3.5 The eligibility view (`utils.eligibility_by_run`)

Eligibility is regrouped into one tree that is easy to read and to build from:
run → zone → checkpoint → sensor block. For each block it lists the readings
that were **included** (all of a reading's fields together, e.g. `accel_x`,
`accel_y` and `accel_z` in one entry) and the ones **excluded** (by reason,
naming the fields). Each checkpoint also lists its **evidence**: its images,
annotated images and finding images, as recorded.

Everything carries a `path` into the records (`records[i].checkpoints[j]...`),
the same form the citations use. Only the layout changes: every value, reason
and count is eligibility's own.

`--eligibility-json output/eligibility.json` writes it, for reading.

### 3.6 The lookup database (`database_lookup.py`)

Built from the eligibility view, kept separate from the derivation database
so a query on one never reads the other. Telemetry samples are left out:
lookups are about checkpoints.

| Table | One row per | Holds |
|---|---|---|
| `runs` | run | run order, which is the latest |
| `zones` | run and zone | the zones in the order the route reaches them |
| `checkpoints` | run and checkpoint | zone, route position, status, result_status (NULL with a reason when left out) |
| `checkpoint_readings` | visit and field | `included` with its value and time, or `excluded` with its reason |
| `evidence` | image | the image path as recorded, its kind (evidence, annotated, finding) |

Every row cites where it sits in the records: a reading's value is
`records[i].checkpoints[j].sensor.environment.temperature_c`, an image is
`records[i].checkpoints[j].evidence_images[k]`. A copy is saved to
`output/b1_lookup_database.sqlite` with its own fingerprint.

---

## 4. The eight derivations

Each derivation is configured in `config/derivations.yaml`; each entry there
is one named **instance** (e.g. `group_mean_temperature_c`). D1–D6 are worked
out separately for every run; D7 and D8 look across runs.

| | Type | What it answers | Example question |
|---|---|---|---|
| D1 | `threshold_compare` | Which checkpoints went over (or stayed under) a fixed limit, and by how much | "Which checkpoints exceeded 30 °C in the latest run?" |
| D2 | `condition_count` | How many items meet a condition, which ones, and which were left out and why (a missed checkpoint is left out, never counted as failed) | "How many checkpoints failed, and which were missed?" |
| D3 | `proportion` | What share of items meet a condition | "What percentage of checkpoints were completed?" |
| D4 | `group_mean` | The average reading per zone, and the checkpoints in each zone | "Mean temperature in rowA_back?" |
| D5 | `group_max` | The highest reading per zone, which checkpoint reached it and when | "Where was the peak vibration?" |
| D6 | `rank_top_n` | The top N readings of the run, highest first, with their checkpoints | "The five hottest checkpoints?" |
| D7 | `run_set_difference` | Which checkpoints the latest run and the one before it did not share | "Did the route change?" |
| D8 | `run_date_range` | When runs took place: the period, the days with runs, runs per day | "On which days were runs taken?" |

Some things none of them hold, on purpose, so the agent abstains rather than
guess: the lowest reading, an average of averages, which items did *not* meet a
condition, gaps between runs, and anything about why something happened.

---

## 5. How a question is answered

### 5.1 The retrieval router (`b1_retrieval_router.py`) — model call 1

Decides which kind of retrieval can answer the question, and nothing else:

| Reply | When | Goes to |
|---|---|---|
| `derivation` | a figure worked out from several values: how many, a share, an average, the highest, a ranking, over a limit, a change between runs, when runs happened | the derivation router |
| `lookup` | recorded values of named items, with nothing worked out: a reading, a status, a zone, the route order, the images | the lookup router |
| `abstain` | why something happened, what will happen, advice, a judgement, or anything the records do not hold | an abstention |

The rule at the boundary: "the temperature at a3_back" is a lookup; "did
a3_back go over the limit" and "the highest temperature in rowA_back" are
derivations. Whether a name exists is not its job: it never abstains because
a name is unfamiliar. `--classify "question"` runs this step alone.

### 5.2 The routers — model call 2

Both routers read the question and say what it is about, without answering
it. They are shown every zone with the checkpoints inside it and the days runs
were taken on, and they name runs, zones and checkpoints the same way (they
share `PLACES`, `resolve_runs` and `check_places`):

- **runs** can be `"all"`, positions (`1` is the oldest, `-1` the latest),
  dates (`"2026-08-21"`), or a span of dates (`"2026-08-01/2026-08-31"`).
  Written dates like 21/8/2026 are read day first.
- **zones** and **checkpoints** are kept apart: "in rowA_back" is a zone, "at
  a3_back" is a checkpoint. A loosely written name is matched to the list.

**The derivation router** (`b1_analytical_router_derived.py`) also names the
derivation instances the question needs:

```json
{"intent": "answer", "runs": [-1], "zones": [], "checkpoints": ["a3_back"],
 "derivations": ["group_mean_temperature_c"]}
```

**The lookup router** (`b1_analytical_router_lookup.py`) names the sensor
fields and the lookup tables instead:

```json
{"intent": "answer", "runs": [-1], "zones": [], "checkpoints": ["a3_back"],
 "fields": ["temperature_c"], "tables": ["checkpoints", "checkpoint_readings"]}
```

Code then checks every name. A run position, date, zone or checkpoint the
records do not hold means the question is about something that is not there,
so the answer is an abstention. A reply that breaks a rule is sent back to the
model with the reason. Every reply is held to a JSON schema by the model
provider itself (structured output), so it cannot come back as prose or with
extra keys.

### 5.3 The SQL writer (`routes/`, `lookups/`) — model call 3

Each derivation type has its own prompt in `routes/`, and each lookup table
has its own in `lookups/`: one file each, all the same shape. A prompt shows
the model exactly its tables (the database's own definitions, comments
included) and explains how to read them: the data hierarchy, what each column
means, which words in a question map to which columns, how to keep ties, and
how to keep an answer complete when a list is empty. The model replies with
one SELECT.

| `lookups/` template | Answers |
|---|---|
| `checkpoints` | whether a checkpoint was completed, its verdict, its zone, the checkpoints in a zone, the route order |
| `checkpoint_readings` | a reading's value, or why there is none; a whole block; every reading at a checkpoint |
| `evidence` | the images at a checkpoint or in a zone, of a kind (annotated, thermal), or why there are none |
| `zones` | the zones a route covered, their order, the zone before or after another, each zone's checkpoints |

The query is run through a **guard** (`database_derivation.run_query`), on
whichever database the path uses:

- only reads are allowed, and only a few harmless functions (no COUNT, SUM,
  AVG, MIN or MAX — every count is already in a table);
- it stops at a time limit and fails if it returns too many rows.

Code then checks the result: every column must have a plain name, each value
must come with a path column, and the runs and checkpoints returned must be
the ones the router named. Anything wrong goes back to the model with the
reason.

If the query returns no rows, code runs the route's own summary query instead
and says so ("the search for this question returned no rows; the stored
result for the runs asked about, shown instead"), so the reader still sees
what the records hold.

### 5.4 Writing the facts (`b1_analytical.py`, `slots.py`)

Code turns the rows into the answer's facts. Each run gets a heading, and each
row becomes one line:

```
Run {{ q0_r0_run_id }}
- checkpoint id {{ q0_r0_checkpoint_id }}, delta {{ q0_r0_delta }}
```

Each `{{ slot }}` is traced to the exact place in the extended record that
holds that value. If a value cannot be traced (the model renamed or calculated
something), the query is sent back.

**Slot filling** then writes the values in, formatted the same way the report
formats them (decimals from `report.yaml`, dates in the run's own time zone),
and records the character span of every value. Those spans are the citations.

**One exception: exclusion reasons.** Why a reading was left out
("sps30_ok=false", "sensor status is offline") is eligibility's verdict, and
no field in the records holds that text. A lookup template can name such
columns in `TEXT_COLUMNS` (only `checkpoint_readings` does: `reason`). They are
written into the facts as code wrote them, with no citation, and verification
skips only those spans. Every value, id, field name and time is still traced.

### 5.5 The prose writer (`b1_analytical_prose.py`) — model call 4

The model sees the question and the filled facts, and writes one or two
sentences to go above them, e.g. "That checkpoint was read in the latest run,
so its temperature is shown below along with when it was taken."

It reads like a person answering: it opens with the answer, joins its parts
with ordinary linking words, and ends by saying how the facts below are laid
out. Its reply may contain **no values and no names at all**: no digits, no
number or ordinal words, no ids, not even a name the question used; it says
"this checkpoint" or "that zone" instead. A reply that breaks this is sent
back with what to write instead. If no reply passes, the facts are shown on
their own.

The facts are marked as data in the prompt, so text copied from the records
(checkpoint names, notes) cannot act as instructions to the model.

### 5.6 Verification (`verification.py`)

The whole answer, lead-in and facts, is checked once more. Every number-like
token is found, classified (measurement, count, identifier, timestamp,
version, ordinal) and matched against the extended record. Text values placed
by slot filling (like a checkpoint name) are checked against their own field.
If anything fails, the answer is not shown.

---

## 6. What can come back

Every answer is wrapped in an **envelope** (`envelope.py`) with one of four
statuses:

| Status | Meaning |
|---|---|
| `OK` | Answered and verified. Also used for an abstention ("the records do not contain this"), which the PDF shows as **ABSTAINED**. |
| `DEGRADED_TEMPLATE_ONLY` | The facts are verified, but the model's lead-in never passed, so the facts are shown alone. |
| `REFUSED_UNVERIFIABLE` | Nothing is shown: the model never produced a valid reply, or the answer failed verification. |
| `PROVIDER_UNAVAILABLE` | The model could not be reached. |

The output holds the `answer`, its `citations` (each a character span and the
path it came from) and `records_consulted` (the runs the values came from).

Each answer is also written as a **PDF** (`b1_answer_pdf.py`) in
`output/b1_answers/`: the question, the answer with every value numbered, where
each value came from, the runs consulted, the verification result and the SQL
that was run. It uses the report's own stylesheet.

**Evidence images.** When the answer cites evidence images, the PDF draws
them under the answer, the way the report draws a checkpoint's evidence: one
grid per run and checkpoint, the RGB and thermal of one view in one cell,
sized down before they are embedded. It reuses the report's own code
(`common.paths.resolve_evidence`, `report.images`). Only the images the answer
cites are drawn. An image whose file cannot be found or read says so in its
cell.

When you run from the command line, a **trace** is printed too: the retrieval
decision, every model reply, whether code accepted or rejected it and why, the
query, and each value with the path it was traced to. This is the first place
to look when an answer is wrong.

---

## 7. Running it

From the project root, with the virtual environment active. All commands need
`PYTHONPATH=src`.

```bash
source venv/bin/activate

# Work out every figure and write output/extended_record.json
PYTHONPATH=src python -m agents.main --all --hide-eligible

# Write the eligibility view (run → zone → checkpoint, kept and left out, evidence) as JSON
PYTHONPATH=src python -m agents.main --eligibility-json output/eligibility.json --hide-eligible

# Only the retrieval router: derivation, lookup or abstain (one model call)
PYTHONPATH=src python -m agents.main --classify "what was the temperature at a3_back?"

# The retrieval router, then the router it picks (two model calls)
PYTHONPATH=src python -m agents.main --router "which checkpoints failed on 21/8/2026?"

# Ask a question end to end: prints the envelope and trace, writes a PDF
PYTHONPATH=src python -m agents.main --ask "which images were taken at a3_back in the latest run?"
```

Other flags, handy while debugging:

| Flag | Does |
|---|---|
| `--field environment.temperature_c` | Show which readings of one field are trusted and which are left out |
| `--verify "TEXT"` | Run verification on a piece of text |
| `--fill "TEXT" --slot ID=PATH` | Fill `{{ ID }}` slots by hand, then verify |
| `--data-dir DIR` | Read runs from another directory |
| `--output FILE` | Write the extended record somewhere else |

`stub.py` at the project root runs both routers over every question in
`config/b1_question_set.yaml` and prints what each one replied:

```bash
PYTHONPATH=src python stub.py                 # the canonical questions
PYTHONPATH=src python stub.py --paraphrases   # and every paraphrase
```

The model API key goes in a `.env` file at the project root
(`ANTHROPIC_API_KEY` or `GEMINI_API_KEY`); it is never written to a config
file or a log.

---

## 8. Configuration

Nothing is read from a config file when a module is imported: `main.py` reads
each file once and passes the settings down.

| File | Holds |
|---|---|
| `config/agent_path.yaml` | Where the runs are, where the evidence images are, and where the extended record, both databases and the PDFs go |
| `config/derivations.yaml` | The derivation instances and their settings, named conditions, eligibility rules, date formats |
| `config/agents.yaml` | The model provider and model, attempts per step, question length limit, verification settings, database limits |
| `config/report.yaml` | Shared with the report: decimal places per field, sensor health flags, stale age, evidence path prefix, image size limit |

In `agent_path.yaml`:

- `data_dir` — the run records (`data_agent`).
- `database` / `lookup_database` — the saved copies of the two databases.
- `evidence_root` — where evidence image paths resolve, after the recorded
  prefix (`/run-files`) is stripped, as the report's `--evidence-root`.
- `b1_answers` — where answer PDFs go.

Settings worth knowing in `agents.yaml`:

- `provider.name` / `provider.model` — Claude or Gemini, and which model. The
  model must accept a temperature setting.
- `agents.b1_analytical.max_attempts` — how many tries each model step gets
  before giving up (the first try included).
- `agents.b1_analytical.max_question_chars` — a longer question is refused,
  never cut short.
- `database.max_rows` / `database.timeout_seconds` — the query guard's limits.
- `verification.numeric_tolerance` — how far a number may be from the record
  and still pass (it is then logged as an anomaly).

---

## 9. Testing

`tests/b1_derivations/` holds one question suite per derivation, `D1_test.py`
to `D8_test.py`. Each case is a question and the status its answer should get:

```python
CASES = [
    ("Which checkpoints went over the temperature limit in the latest run?", "OK"),
    ("Mean humidity per zone in the latest run?", "ABSTAINED"),
]
```

They call the real model, so they need the API key. Every question now goes
through the retrieval router first, so a derivation question it sends to
lookup shows up in these suites too. For every case they write the answer PDF
and a JSON file with the envelope and the full trace to
`tests/output/b1/<derivation>/`, before the check, so a failing case can
always be read afterwards.

```bash
PYTHONPATH=src pytest tests/b1_derivations -v
PYTHONPATH=src pytest tests/b1_derivations/D4_test.py -v
```

Question lists to run by hand:

- `B1_QUESTIONS.txt` — questions across the eight derivations, with the
  expected result.
- `B1_LOOKUP_QUESTIONS.txt` — twenty questions for the retrieval router, each
  lookup template, the boundary with derivations, and abstentions.

Some questions can reasonably be answered by more than one derivation (the
single highest reading is in both D5 and D6); the trace shows which route was
taken.

---

## 10. Where things live

| File | Job |
|---|---|
| `main.py` | The command line: reads config, runs the steps asked for |
| `data_agent.py` | Finds and loads the run records, oldest first |
| `derivation.py` | The exclusion rule and the eight derivations; builds the extended record |
| `database_derivation.py` | Builds the derivation database; the query guard; saving and reusing a database copy |
| `database_lookup.py` | Builds the lookup database from the eligibility view |
| `b1_analytical.py` | Runs B-1 end to end: retrieval router, router, SQL writer, facts, prose, verification |
| `b1_retrieval_router.py` | Model call 1: derivation, lookup or abstain |
| `b1_analytical_router_derived.py` | Model call 2 for a derivation: runs, zones, checkpoints, derivations |
| `b1_analytical_router_lookup.py` | Model call 2 for a lookup: runs, zones, checkpoints, fields, tables |
| `routes/` | Model call 3 for a derivation: one SQL writer prompt per derivation type |
| `lookups/` | Model call 3 for a lookup: one SQL writer prompt per lookup table |
| `b1_analytical_prose.py` | Model call 4: the lead-in sentence |
| `slots.py` | Writes values into templates and records where each one landed |
| `verification.py` | Checks every number-like token against the records |
| `envelope.py` | The shape every answer is returned in |
| `b1_answer_pdf.py`, `templates/b1_answer.j2` | The answer PDF, with evidence images |
| `provider.py` | Talks to Claude or Gemini over their REST APIs |
| `schema.py` | The agent's data types |
| `utils.py` | Shared helpers: formatting, config parsing, printing, the eligibility view |
| `DECISIONS.md` | Why things are the way they are, and where the design goes beyond the brief |

---

## 11. Changing things safely

- **Add an instance of an existing derivation** (e.g. a D4 mean for humidity):
  add an entry to `config/derivations.yaml`. The derivation, its database
  table, the router's description and the SQL writer's tables all follow from
  it. The field needs a decimals entry in `report.yaml`.
- **Change what a derivation outputs**: change `derivation.py`, then its
  loader and table in `database_derivation.py`, then its prompt in `routes/`.
  The table definitions are shown to the model as they are, so their comments
  are part of the prompt.
- **Add a lookup table**: add its DDL and loader to `database_lookup.py`, its
  name to `TABLES` there, a description to `DESCRIPTIONS` in
  `b1_analytical_router_lookup.py`, and a template in `lookups/` (registered
  in `lookups/__init__.py`). Give every row a `path` into the records, so its
  values can be cited.
- **Turn a derivation route off for a while**: comment out its line in
  `routes/__init__.py`. Neither router offers it then; its figures are still
  worked out and stored.
- **Change a prompt**: prompts are plain text in `b1_retrieval_router.py`,
  both routers, `routes/*.py`, `lookups/*.py` and `b1_analytical_prose.py`.
  Keep them free of real zone and checkpoint names: examples use `<zone>` and
  `<checkpoint>`, and the real names come from the records at run time.
  Re-run the relevant test suite afterwards.
- **The records route** (answering from the raw records rather than a
  derivation) exists in `routes/records.py` but is switched off in the router
  for now; the lines to switch it back on are commented in
  `b1_analytical_router_derived.py`.
- **Record why**: when a change goes beyond the brief or makes a non-obvious
  choice, add a note to `DECISIONS.md`.
