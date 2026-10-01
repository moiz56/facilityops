# The agent layer

This folder turns inspection run records into words a facility manager can
use. It holds three agents:

| Agent | What it does | Calls a model? |
|---|---|---|
| **B-1 Analytical** | Answers questions in plain English: "which checkpoints went over the temperature limit in the latest run?" | Yes: to understand the question and point at the answer |
| **B-2 Narrative** | Writes the prose inside the report: an executive summary, a coverage statement, an introduction per zone, a note per checkpoint | Only to order the summary's paragraphs (optional) |
| **B-3 Action plan** | Turns a run's findings into a prioritised, grouped list of actions | No (an optional opening sentence, off by default) |

**The one rule everything is built around: the model never writes a number.**
Code finds every value, writes it into a template, and checks the finished
text before anyone sees it. Every value carries a citation to the exact place
in the records it came from. If a number cannot be traced, the text is refused
rather than shown.

---

## Contents

1. [The data](#1-the-data)
2. [How data flows](#2-how-data-flows)
3. [Quick start](#3-quick-start)
4. [B-1: asking questions](#4-b-1-asking-questions)
5. [B-2: the report's prose](#5-b-2-the-reports-prose)
6. [B-3: the action plan](#6-b-3-the-action-plan)
7. [Putting B-2 and B-3 in the report](#7-putting-b-2-and-b-3-in-the-report)
8. [What every agent returns](#8-what-every-agent-returns)
9. [Configuration](#9-configuration)
10. [Testing](#10-testing)
11. [Where things live](#11-where-things-live)
12. [Changing things safely](#12-changing-things-safely)

---

## 1. The data

An inspection robot drives a **route** around a facility. Each drive is a
**run**, saved as a `run.json` record. Each run is one hierarchy:

```
run                     one drive of the route
└─ zone                 an area of the route, e.g. a row of racks
   └─ checkpoint        one stop in that zone, e.g. a single rack
      ├─ status         did the robot get there: COMPLETED or MISSED
      ├─ result_status  the verdict: PASS, FAIL or WARN
      ├─ readings       accelerometer, environment, particulate
      └─ evidence       the images captured there (RGB and thermal)
```

A run also holds **findings** (what the robot noticed, with a severity and a
recommended action), **sensor alerts**, **telemetry samples** (a reading every
couple of seconds while driving) and an **event log**.

Two ideas matter everywhere:

- **Missed is not failed.** A MISSED checkpoint was never reached, so it has no
  real verdict. A FAILED one was reached and judged FAIL.
- **Not every reading can be trusted.** A reading from a sensor that was
  offline, disconnected or stale, or from a missed checkpoint, is *left out*
  of every figure, and the reason is kept. This is called **eligibility**.

---

## 2. How data flows

```
                     run.json files
                           │
                           ▼
                  load and parse (common/)
                           │
                           ▼
          eligibility: which readings can be trusted
                           │
                           ▼
       the derivations: figures worked out by code only
       (means, maxima, counts, thresholds, rankings, ...)
                           │
                           ▼
        EXTENDED RECORD = the records + every figure
                           │
        ┌──────────────────┼──────────────────────┐
        ▼                  ▼                      ▼
      B-1                 B-2                    B-3
  all runs at once    one run at a time      one run at a time
  two databases,      adds its own counts    groups the run's
  a question in,      ("run values"), fills  findings into
  an answer + PDF     templates, verifies    actions, verifies
        │                  │                      │
        ▼                  ▼                      ▼
   answer PDF        report prose            action plan
   output/b1_answers  (or CLI output)        (or CLI output)
```

**The derivations** are the only place arithmetic happens. There are eight
kinds, fixed by the brief:

| # | Derivation | Gives |
|---|---|---|
| D1 | threshold compare | each checkpoint's reading against a limit, and how far over or under |
| D2 | condition count | how many items meet a condition (e.g. result FAIL), and which |
| D3 | proportion | the share of items meeting a condition |
| D4 | group mean | the mean of a reading per zone |
| D5 | group max | the highest reading per zone, and where it was |
| D6 | rank top N | the N highest readings |
| D7 | run set difference | which checkpoints only one of the last two runs had |
| D8 | run date range | when runs were taken, and on which days |

B-1 and B-2/B-3 use **separate derivation settings**: B-1 reads
`config/derivations.yaml` over the whole corpus; B-2 and B-3 read
`config/b2_derivations.yaml` over the one run they were given. They never run
together, so changing one never affects the other.

---

## 3. Quick start

From the project root:

```bash
source venv/bin/activate
```

Put the model's API key in a `.env` file at the project root (it is never
written to a config file or a log):

```
ANTHROPIC_API_KEY=...        # or GEMINI_API_KEY, matching agents.yaml provider.name
```

Every command runs as `PYTHONPATH=src python -m agents.main ...`. The most
useful ones:

```bash
# B-1: ask a question (all runs in data_agent/)
PYTHONPATH=src python -m agents.main --ask "which checkpoints failed in the latest run?"

# B-2: the four prose parts, for one run (a run directory or its run.json)
PYTHONPATH=src python -m agents.main --summary         data_agent/<run_dir>
PYTHONPATH=src python -m agents.main --coverage        data_agent/<run_dir>
PYTHONPATH=src python -m agents.main --section-intros  data_agent/<run_dir>
PYTHONPATH=src python -m agents.main --item-notes      data_agent/<run_dir>

# B-3: the action plan, for one run
PYTHONPATH=src python -m agents.main --plan data_agent/<run_dir>

# The report, with whichever agent parts are switched on in report.yaml
PYTHONPATH=src python -m report.cli --record data_agent/<run_dir>/run.json
```

---

## 4. B-1: asking questions

B-1 answers questions over **all the runs** in the data directory. It answers
three kinds:

| Kind | Example | Comes from |
|---|---|---|
| A figure | "which checkpoints went over the temperature limit?", "mean vibration per zone" | a derivation |
| A recorded detail | "which findings need review?", "what was missed and why?", "how did the latest run end?" | the run's own records |
| A recorded value of one item | "the temperature at a3_back", "which images were taken there?", "which zone is a3_back in?" | the lookup tables |

Anything else (why something happened, a prediction, a judgement, something
the records do not hold) gets the answer **"the records do not contain this"**.

### How a question is answered

```
question
   │
   ▼  model 1  retrieval router: a figure/record, a lookup, or abstain?
   ▼  model 2  router: which runs, zones, checkpoints, and which figure or table
   ▼  model 3  SQL writer: one SELECT over the right database
   ▼  code     guard: read-only, a few safe functions, a row limit
   ▼  code     the rows become facts; every value is traced to its path
   ▼  model 4  prose writer: one or two sentences, no values, no names
   ▼  code     verification: every number checked against the records
answer + citations + PDF
```

B-1 keeps **two SQLite databases**, built once and reused until the data or
code changes:

- the **derivation database**: every figure, plus the run's own records
  (checkpoints, findings, alerts, events)
- the **lookup database**: each checkpoint's readings (kept or left out, and
  why), status, zone and evidence

### Commands

```bash
# Ask end to end: prints the answer, its envelope and a full trace, writes a PDF
PYTHONPATH=src python -m agents.main --ask "QUESTION"

# Only the first step: derivation, lookup or abstain
PYTHONPATH=src python -m agents.main --classify "QUESTION"

# The first two steps: prints the route it chose
PYTHONPATH=src python -m agents.main --router "QUESTION"

# Work out every figure and write output/extended_record.json
PYTHONPATH=src python -m agents.main --all --hide-eligible

# Write the eligibility view (run → zone → checkpoint, kept and left out)
PYTHONPATH=src python -m agents.main --eligibility-json output/eligibility.json --hide-eligible
```

The answer PDF goes to `output/b1_answers/`. It shows the question, the answer
with every value numbered, where each value came from, the verification
result, the SQL that ran, and any evidence images the answer cites.

When an answer looks wrong, read the **trace** printed by `--ask`: every model
reply, whether code accepted it and why, the query, and each value's path.

---

## 5. B-2: the report's prose

B-2 writes four parts for **one run**. Code writes every sentence from a
template in `templates/`; each value goes in as a cited slot.

| Part | Where it goes in the report | What it says |
|---|---|---|
| **Executive summary** | first page after the cover | when and how the run went, what passed and failed (failed checkpoints grouped), the headline zone mean, and short pointers to the coverage statement |
| **Coverage statement** | below the summary, same page | completion and every missed checkpoint with its reason, evidence and thermal coverage, images referenced and resolved, sensor availability, and every count the record declares that disagrees with its data |
| **Section introduction** | under each zone heading | how many checkpoints the zone covers, samples recorded, and each mean with usable/excluded samples and its rank among zones |
| **Item note** | at the top of each checkpoint | one of: missed (with reason), no evidence (with findings), a sensor warning (with the reading), or normal (what was photographed) |

**The model's only job** is to choose the order of the summary's paragraphs. It
sees what each paragraph is about, never its text. With
`b2_narrative.prose: false`, or no API key, code's order is used instead. The
other three parts never call a model.

B-2 adds its own **run values** (counts no derivation gives, formatted dates,
per-checkpoint and per-zone figures) to the run's extended record, so they
can be cited like any other field.

### Commands

```bash
PYTHONPATH=src python -m agents.main --summary        data_agent/<run_dir>
PYTHONPATH=src python -m agents.main --coverage       data_agent/<run_dir>
PYTHONPATH=src python -m agents.main --section-intros data_agent/<run_dir>
PYTHONPATH=src python -m agents.main --item-notes     data_agent/<run_dir>
```

Each prints the envelope(s) as JSON, then the text, and writes the run's
extended record to `output/b2/<run_id>_extended_record.json`.

---

## 6. B-3: the action plan

B-3 turns **one run's findings** into actions. It never writes an action: each
finding already carries a `recommended_action`, and B-3 carries it **word for
word**. What it adds:

- **a category** for each finding type, from `config/action_mapping.yaml`
- **a priority**: severity first (fail, then warning, then info), then the
  category's rank, then route order. Equal priorities are allowed.
- **grouping**: findings of one type with the same action become one row
  ("4 findings at a5_back, a6_back, ...") instead of repeating the action
- **unmapped types**: a finding type with no configured category is stated
  plainly, never guessed. `airflow_obstruction` is deliberately unmapped.

B-3 makes **no model call** by default (`b3_action.prose: false`). Switched on,
the model adds one opening sentence and nothing else.

### Command

```bash
PYTHONPATH=src python -m agents.main --plan data_agent/<run_dir>
```

It prints the envelope and the plan as text, and writes
`output/b2/<run_id>_plan_extended_record.json`. Use `--mapping PATH` for a
different action mapping.

---

## 7. Putting B-2 and B-3 in the report

The report engine (`src/report`) calls the agents for the run it renders.
Each part has its own switch under `sections:` in `config/report.yaml`:

| Switch | Puts in the report |
|---|---|
| `executive_summary: true` | the executive summary, after the cover |
| `coverage_statement: true` | the coverage statement, below the summary |
| `action_plan: true` | the action plan as a table, after the summary page |
| `section_intros: true` | an introduction under each zone heading |
| `item_notes: true` | a note at the top of each checkpoint |

Then generate the report as usual:

```bash
PYTHONPATH=src python -m report.cli --record data_agent/<run_dir>/run.json
```

The report order with everything on: cover, executive summary and coverage
statement, action plan, contents, coverage, findings, alerts, zone telemetry,
checkpoints (with intros and notes), run summary.

If an agent is switched off in `agents.yaml`, refuses its text, or fails, the
report is still produced without that part, and the log says why. A part is
never shown empty or half-written.

---

## 8. What every agent returns

Every output is wrapped in the same **envelope** (`envelope.py`):

```json
{
  "agent": "B-2",
  "agent_version": "0.2.0",
  "model": null,
  "prompt_version": "narrative_v1",
  "source_run_ids": ["<run_id>"],
  "generated_at": "2026-10-01T09:00:00Z",
  "status": "OK",
  "output": { "text": "...", "citations": [ { "claim_span": [56, 78], "source_field": "..." } ] },
  "verification": { "method": "deterministic", "numeric_tokens_emitted": 7, "numeric_tokens_verified": 7, ... }
}
```

| Status | Meaning |
|---|---|
| `OK` | Produced and verified. For B-1 this also covers "the records do not contain this". |
| `DEGRADED_TEMPLATE_ONLY` | Verified, but the model's part was skipped (no provider, or no valid reply), so code's version is shown. |
| `REFUSED_UNVERIFIABLE` | Nothing is shown: a value could not be traced to the records. |
| `PROVIDER_UNAVAILABLE` | The model could not be reached (B-1). |

`model` is `null` and `method` is `deterministic` whenever no model was called.
That is the normal case for B-3 and for three of B-2's four parts.

---

## 9. Configuration

Nothing is read from a config file when a module is imported: the entry point
reads each file once and passes the settings down.

| File | Holds | Used by |
|---|---|---|
| `config/agent_path.yaml` | where the runs, evidence images and outputs are | all |
| `config/agents.yaml` | the model provider, each agent's switches, verification settings, database limits | all |
| `config/derivations.yaml` | the derivation instances B-1 offers | B-1 |
| `config/b2_derivations.yaml` | the per-run derivations B-2 and B-3 use | B-2, B-3 |
| `config/action_mapping.yaml` | finding type → category, category ranks, severity ranks | B-3 |
| `config/report.yaml` | shared with the report: decimals, units, sensor flags, which report sections are on | all |

Settings you are most likely to change:

| Setting | In | Does |
|---|---|---|
| `provider.name`, `provider.model` | `agents.yaml` | Claude or Gemini, and which model (it must accept a temperature) |
| `agents.<agent>.enabled` | `agents.yaml` | switch an agent off entirely |
| `agents.<agent>.prose` | `agents.yaml` | allow or forbid that agent's model call |
| `agents.b2_narrative.warning_fields`, `metric_names` | `agents.yaml` | which reading a sensor warning is about, and how a sentence names it |
| `categories` | `action_mapping.yaml` | category ranks for B-3's priority (placeholders until the client confirms) |
| `units`, `decimals` | `report.yaml` | how a value prints, e.g. `temperature_c: "°C"` |
| `data_dir`, `evidence_root`, `b2_output` | `agent_path.yaml` | where runs, images and B-2/B-3 output live |

---

## 10. Testing

`tests/b1_derivations/` holds one question suite per derivation
(`D1_test.py` to `D8_test.py`). Each case is a question and the status its
answer should get. They call the real model, so they need the API key. Each
case writes its answer PDF and full trace to `tests/output/b1/` before it is
checked, so a failing case can be read afterwards.

```bash
PYTHONPATH=src pytest tests/b1_derivations -v
PYTHONPATH=src pytest tests/b1_derivations/D4_test.py -v
```

`stub.py` at the project root is the **question-set suite**: every question
in `config/b1_question_set.yaml` (the brief's twenty, TB-04), answered end to
end. A must-abstain question passes when it abstains; every other one passes
when it is answered (OK, or DEGRADED_TEMPLATE_ONLY). PDFs and traces go to
`tests/output/b1/question_set/`.

```bash
PYTHONPATH=src pytest stub.py -v                        # the 20 canonical questions
PYTHONPATH=src pytest stub.py -v -k Q07                 # one question
B1_PARAPHRASES=1 PYTHONPATH=src pytest stub.py -v       # and every paraphrase
```

More questions to try by hand: `B1_QUESTIONS.txt` and `B1_LOOKUP_QUESTIONS.txt`.

---

## 11. Where things live

**Shared**

| File | Job |
|---|---|
| `main.py` | the command line |
| `data_agent.py` | loads every run record, oldest first |
| `derivation.py` | eligibility and the eight derivations; builds the extended record |
| `slots.py` | writes values into templates and records where each landed |
| `verification.py` | checks every number-like token in a text against the records |
| `envelope.py` | the shape every output is returned in |
| `provider.py` | talks to Claude or Gemini |
| `schema.py`, `utils.py` | data types; shared helpers and config parsing |

**B-1**

| File | Job |
|---|---|
| `b1_analytical.py` | runs a question end to end |
| `b1_retrieval_router.py` | model call 1: figure/record, lookup or abstain |
| `b1_analytical_router_derived.py`, `b1_analytical_router_lookup.py` | model call 2: what the question is about |
| `routes/`, `lookups/` | model call 3: one SQL prompt per derivation type or lookup table |
| `b1_analytical_prose.py` | model call 4: the lead-in sentence |
| `database_derivation.py`, `database_lookup.py` | the two databases and the query guard |
| `b1_answer_pdf.py` | the answer PDF |

**B-2 and B-3**

| File | Job |
|---|---|
| `b2_narrative.py` | the four prose parts and their run values |
| `b3_action.py` | the action plan |
| `templates/` | one template per part: `executive_summary.j2`, `coverage_statement.j2`, `section_intro.j2`, `item_note.j2`, `b3_action.j2`, `b1_answer.j2` |

`DECISIONS.md` records why things are the way they are, and where the design
goes beyond the brief.

---

## 12. Changing things safely

- **Change wording** in B-2 or B-3: edit its template in `templates/`. Keep
  digits and number words out of the template's own text; every value must go
  in as a slot, or verification refuses the text.
- **Add a figure B-1 can answer**: add an instance to `config/derivations.yaml`.
  Its database table, the router's description and the SQL writer follow from
  it. The field needs a `decimals` entry in `report.yaml`.
- **Add a figure B-2 uses**: add it to `config/b2_derivations.yaml`. B-1 is
  unaffected.
- **Map a new finding type** for B-3: add it under `features` in
  `config/action_mapping.yaml`. Never map a type because its name sounds like
  another.
- **Change a prompt**: prompts are plain text in the B-1 modules, `routes/`
  and `lookups/`. Use `<zone>` and `<checkpoint>` in examples, never real
  names. Re-run the relevant test suite afterwards.
- **Record why**: when a change goes beyond the brief or makes a non-obvious
  choice, add a note to `DECISIONS.md`.
