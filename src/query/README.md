# Ask the Record (Milestone 3)

This folder is the customer query surface. A customer types a question about
their facility in their own words. They get back an answer built from their
own records, with a citation for every value. Or they are told plainly that
the records do not contain it.

Milestone 3 adds four things on top of the agent layer (`src/agents`):

| # | Feature | In one line | Where the code is |
|---|---|---|---|
| 1 | Ask the record | A question in plain words gets a cited answer, or "the records do not contain this" | `src/query/ask.py`, `src/query/agreed_route.py`, `src/router/` |
| 2 | Per-tenant views | A tenant can only ever reach its own records | `src/query/scope.py`, `config/tenants.yaml` |
| 3 | "Could not read" | Something captured but not readable is its own category | `src/common/readability.py`, `config/report.yaml` |
| 4 | Row sign-off | Each row gets a confidence figure worked out from coverage | `src/common/signoff.py`, `config/signoff/` |

The rules from Milestone 2 still hold. The model never writes a number. It
never explains why something happened, never forecasts, and never says
something got better or worse. Every number is checked before the answer is
returned.

---

## Contents

1. [Status as of 2026-10-31](#1-status-as-of-2026-10-31)
2. [Quick start](#2-quick-start)
3. [How a question is answered](#3-how-a-question-is-answered)
4. [Tenant isolation](#4-tenant-isolation)
5. [The answer envelope](#5-the-answer-envelope)
6. [The query log](#6-the-query-log)
7. [The intents: 49 questions](#7-the-intents-49-questions)
8. [The confidence router](#8-the-confidence-router)
9. [New and extended derivations](#9-new-and-extended-derivations)
10. ["Could not read"](#10-could-not-read)
11. [Row sign-off (section 6)](#11-row-sign-off-section-6)
12. [How to word a question](#12-how-to-word-a-question)
13. [Files in this milestone](#13-files-in-this-milestone)
14. [Configuration reference](#14-configuration-reference)
15. [Open items](#15-open-items)

---

## 1. Status as of 2026-10-31

The milestone is being finished.

| Item | State |
|---|---|
| Tenant isolation at load time | Done |
| Append-only query log, one file per tenant | Done |
| Intent set: 49 intents (20 original + 29 new), 5+ paraphrases each | Done |
| Intent results, `--use both`, tenant `tenant_facility` | **47 of 49 pass** (Q28 and Q38 fail, see [7.4](#74-results)) |
| Confidence router, first step (lookup / derivation / abstain) | Done, on |
| Confidence router, second step (which derivation or table) | Done, **off** in config right now |
| New derivations D9, D10, D11, plus extended D1, D2, D7 | Done |
| could_not_read in the report and the manifest | Done |
| could_not_read reason codes | 1 of 6 has a producer; 5 are placeholders |
| Row sign-off in the report and the manifest | Done |
| Row `expected_subjects` | Placeholders until the client sends real counts |
| TQ-01 to TQ-22 test suite | Not built yet |
| 60-question transcript (40 canonical + 20 adversarial) | Not built yet |

---

## 2. Quick start

### Fresh setup (Ubuntu)

For someone who has just pulled the repo.

**1. System packages.** Python 3.11 or newer, and the libraries WeasyPrint
needs to write the answer PDFs (pango, cairo, gdk-pixbuf):

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip git
sudo apt install libpango-1.0-0 libpangoft2-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 fonts-liberation
python3 --version    # must be 3.11 or newer
```

Ubuntu 22.04 ships Python 3.10. On 22.04, install 3.11 first
(`sudo apt install python3.11 python3.11-venv`) and use `python3.11` in step 2.

**2. Clone and install.**

```bash
git clone <repo-url> facilityops-report
cd facilityops-report
python3 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"
```

This installs every Python library, the router's included
(`semantic-router==1.1.1`, `fastembed==0.9.1`). If your venv already exists
and only the router libraries are missing:

```bash
pip install semantic-router==1.1.1 fastembed==0.9.1
```

**3. The API key.** `.env` is not in git. Create it in the project root:

```bash
echo "ANTHROPIC_API_KEY=your-key-here" > .env
```

The key must match `provider.name` in `config/agents.yaml` (`claude` now).
For Gemini it is `GEMINI_API_KEY`.

**4. The tenant data.** `data_tenant/` is not in git. Get a copy and put it in
the project root, laid out as `data_tenant/<facility_id>/<run_id>/run.json`
(see [4.3](#43-how-to-structure-the-files)). Without it, every question gets
"no runs in scope".

**5. Internet, once.** The first router run downloads the encoder model
(`BAAI/bge-small-en-v1.5`). After that it works offline.

**6. Ask a question.**

```bash
PYTHONPATH=src python -m query.agreed_route --tenant tenant_racks --ask "which zone is a3_back in"
```

Already in git, so nothing to do:

- the saved router files (`src/router/*.yaml`), so no refit is needed
- all config, including `tenants.yaml` and `config/signoff/`
- `logs/` and `output/`, which are made on first use

Keep `PYTHONPATH=src` on every command, even after `pip install`. The
package settings in `pyproject.toml` include only `src/common` and
`src/report`, so `query`, `agents` and `router` are found through
`PYTHONPATH`.

### Every time after that

Run everything from the project root, with the virtual environment active.

```bash
cd facilityops-report
source venv/bin/activate
```
The encoder (`BAAI/bge-small-en-v1.5`) runs locally. It needs no key.

### Ask one question

There are two ways in. Use the first one for customers.

```bash
# Model + confidence router. The router must back the model's route, or the
# question is unroutable. The answer carries routing_confidence.
PYTHONPATH=src python -m query.agreed_route --tenant tenant_racks --ask "which zone is a3_back in"

# Model only. No confidence check, no routing_confidence in the answer.
PYTHONPATH=src python -m query.main --tenant tenant_racks --ask "how many runs are there"
```

Both take the same options:

| Option | Meaning |
|---|---|
| `--tenant` | Required. The tenant asking, as named in `config/tenants.yaml` |
| `--ask` | Required. The question |
| `--facility` | Limit to one facility the tenant owns. Repeat it for more. Default: all of them |
| `--from`, `--to` | First and last run date, `YYYY-MM-DD`, both ends included. Use both or neither |
| `--asked-by` | Who is asking. Default `cli`. Goes in the query log |

### Ask the whole question set

```bash
# Every question and every paraphrase, one line each
PYTHONPATH=src python -m query.run_question_set --use router
PYTHONPATH=src python -m query.run_question_set --use model --tenant tenant_racks
PYTHONPATH=src python -m query.run_question_set --use both  --tenant tenant_racks --ids Q01 Q24 --canonical-only

# Each canonical question once, full answers kept in a file, plus one PDF per answer
PYTHONPATH=src python -m query.run_milestone3
PYTHONPATH=src python -m query.run_milestone3 --use both --tenant tenant_facility
PYTHONPATH=src python -m query.run_milestone3 --ids Q28 Q38
```

The three `--use` modes:

| Mode | What runs | Needs a tenant? | Calls the model? |
|---|---|---|---|
| `router` | The saved semantic routers only. Shows the route and its confidence | No | No |
| `model` | `query.ask`, the model alone | Yes | Yes |
| `both` | `query.agreed_route`, the model checked by the routers | Yes | Yes |

`run_milestone3` writes `output/milestone3/answers_<use>_<time>.txt` and,
for `model` and `both`, one PDF per question in `output/milestone3/<use>/`.
It writes the PDFs whatever `agents.yaml` says about `pdf`.

A question **passes** when:

- it must abstain, and it was abstained or unroutable, or
- it can be answered, and it was answered (status `OK` or
  `DEGRADED_TEMPLATE_ONLY`).

### Refit the routers

Do this after you change the question sets, `src/router/question_set_labels.yaml`
or the hand-written lists in the router files.

```bash
python src/router/first_step_router.py                          # fit, test, save
python src/router/first_step_router.py "your question"          # route one question
PYTHONPATH=src python -m router.second_step_router              # fit, test, save both second-step routers
PYTHONPATH=src python -m router.second_step_router derivation "your question"
PYTHONPATH=src python -m router.second_step_router lookup "your question"
```

---

## 3. How a question is answered

The order is fixed: **scope first, derive second, answer third.**

```
question + tenant
      │
      ▼
1. scope.resolve ── only this tenant's runs are loaded. Nothing else exists from here on.
      │
      ▼
2. eligibility, extended record, both SQLite databases ── built from those runs only
      │
      ▼
3. First step. The model picks: lookup, derivation, catalogue or abstain.
   The first-step router gives its confidence for that same pick.
   Below the pick's minimum ──► unroutable
      │
      ▼
4. Second step (lookup and derivation only). The model picks the tables or
   derivations. If second_step_check is on, the second-step router gives a
   confidence for each pick. Any pick below its minimum ──► unroutable
      │
      ▼
5. B-1 answers, exactly as in Milestone 2: SQL writer, slot filling,
   verify_numeric, citations. The model's checked picks are replayed, not asked again.
      │
      ▼
6. Envelope + intent + routing_confidence + records_in_scope
   ──► appended to logs/<tenant_id>.logs ──► printed ──► answer PDF
```

The model's pick is always the one taken. The router does not choose the
route. It only says how sure it is about the model's choice. This way a
question can never be sent down a route the model did not pick, and a weak
match is never answered.

Each model router is called once. `agreed_route.Replay` hands the replies
that were checked back to `answer_question`, so the steps that were checked
are the steps the answer takes.

---

## 4. Tenant isolation

### 4.1 The rule

A tenant can see its own records and nothing else. This is enforced when the
records are **loaded**, not when the answer is written.

`scope.resolve` runs before any routing, deriving or model call. Records that
belong to another tenant are never read into the process. So no answer,
count, mean, ranking or date range can include them. This is what the brief
asks for: filtering the answer afterwards is not allowed, because an
aggregate over the whole corpus has already leaked.

`scope.py` is the only module that takes a `tenant_id` or loads records.

### 4.2 How it works, step by step

1. `tenant_facilities` reads the whole mapping in `config/tenants.yaml`. If
   one facility is listed under two tenants, it refuses. It does not guess.
2. If the tenant is not in the mapping, it refuses (`ScopeError`).
3. If `--facility` names a facility the tenant does not own, it refuses.
4. Only the allowed facilities' folders are read. Other tenants' folders are
   never opened.
5. Every loaded record is checked again by the `facility_id` **inside the
   record**. A run filed in the wrong folder is dropped, not shown.
6. If there is a run window, a run is kept only if it started inside it, by
   its recorded local date. A run with no `start_time` is outside any window.
7. If nothing is left, the answer is "the records do not contain this" with
   `records_in_scope: 0`, and the question is still logged.

### 4.3 How to structure the files

The tenant corpus lives in `data_tenant/` (set by `data_dir` in
`config/tenants.yaml`). It is gitignored.

```
data_tenant/
├── sis_racks_checkpoint_route/                         ← one folder per facility_id
│   ├── 20260730_145833-sis_racks_checkpoint_route/     ← one folder per run
│   │   ├── run.json                                    ← the run record
│   │   ├── evidence/
│   │   ├── images/
│   │   └── ...
│   └── 20260730_150539-sis_racks_checkpoint_route/
│       └── run.json
├── sis_facility_checkpoint_route/
│   └── 20260728_144120-sis_facility_checkpoint_route/
│       └── run.json
└── ...
```

Rules:

- The folder name must be the facility's `facility_id`. That is how scope
  finds which folders to read.
- The record path inside a facility folder is set by `record_patterns`. Today
  it is `{facility_id}/*/run.json`. `{facility_id}` is required in every
  pattern.
- The folder is only a shortcut. What decides ownership is the
  `facility_id` written in `run.json`. A run of facility A put in facility
  B's folder is dropped.

### 4.4 `config/tenants.yaml`

```yaml
data_dir: data_tenant              # the tenant corpus, relative to the project root
record_patterns:
  - "{facility_id}/*/run.json"     # data_tenant/<facility_id>/<run>/run.json

tenants:
  tenant_facility:
    facility_ids: [sis_facility_checkpoint_route]
  tenant_racks:
    facility_ids: [sis_racks_checkpoint_route]
  # ... one entry per tenant
```

Today there is one tenant per facility. The `sim_` tenants are simulator
output with made-up readings: fine for checking the schema, not for analysis.

**To add a tenant:** put its runs in `data_tenant/<facility_id>/<run>/run.json`,
then add an entry under `tenants` listing its `facility_ids`. A tenant can own
more than one facility. A facility can belong to only one tenant.

---

## 5. The answer envelope

The envelope is the one from Milestone 2. Its `output` keeps B-1's body
(`answer`, `citations`, `records_consulted`) and adds these keys:

| Key | Meaning |
|---|---|
| `intent` | The category the answer falls in (see [7.3](#73-answer-categories)). `null` when unroutable |
| `routing_confidence` | The first-step router's confidence for the model's pick. `null` for a catalogue question. Only from `query.agreed_route` |
| `second_step_confidence` | One confidence per second-step pick, or `null` when there was no second step. **Only present when `second_step_check` is on** |
| `records_in_scope` | How many runs this tenant's scope held. Always present |

Example shape (`query.agreed_route`, second step off; values are illustrative):

```json
{
  "agent": "B-1",
  "status": "OK",
  "output": {
    "answer": "...",
    "citations": [ ... ],
    "records_consulted": ["20260730_074644-sis_facility_checkpoint_route"],
    "intent": "retrieval",
    "routing_confidence": 0.76,
    "records_in_scope": 7
  },
  "verification": { ... }
}
```

An unroutable question (illustrative):

```json
"output": {
  "answer": "The records do not contain this. For checkpoint_4 the records hold inspection results, sensor readings, evidence captures and findings.",
  "citations": [],
  "records_consulted": ["...every run in scope..."],
  "intent": null,
  "routing_confidence": 0.12,
  "records_in_scope": 7
}
```

The second sentence lists what the records hold about the checkpoint or zone
the question names. It only appears when a name in the question is a real
`checkpoint_id` or `zone` in these records. A checkpoint is looked for before
a zone, so a name that is both (like `home`) is read as the checkpoint. What
can be listed: inspection results, sensor readings, evidence captures,
findings and operator notes.

---

## 6. The query log

Every question is appended as one JSON line to `logs/<tenant_id>.logs`
(folder set by `agent.query_logs` in `config/agent_path.yaml`). Each tenant
has its own file. An entry looks like this (values illustrative):

```json
{"tenant_id": "tenant_racks", "asked_by": "cli", "asked_at": "2026-10-31T09:12:44Z",
 "question": "which zone is a3_back in", "intent": "retrieval",
 "route": "lookup", "routed_to": ["checkpoints"], "records_in_scope": 87,
 "status": "OK", "answer": "..."}
```

| Key | Meaning |
|---|---|
| `route` | The model's first step: `derivation`, `lookup`, `catalogue` or `abstain` |
| `routed_to` | The model's second step: derivation names or lookup tables. `null` if it abstained or never ran |

The log is append-only. `query_log.py` only writes. Nothing in it reads a log
back, so nothing reachable through `ask` can return one.

Logged: answered, abstained, unroutable, and "no runs in scope" questions.
Not logged yet: see [Open items](#15-open-items).

---

## 7. The intents: 49 questions

### 7.1 Where they live

The intents are the questions in two files, in the same YAML format:

| File | Ids | Count |
|---|---|---|
| `config/b1_question_set.yaml` | Q01 to Q20 | 20 (the original set: 16 answerable, 4 must abstain) |
| `config/b1_question_set_extension.yaml` | Q21 to Q49 | 29 new (23 answerable, 6 must abstain) |
| **Total** | | **49** (39 answerable, 10 must abstain) |

The brief asks for at least 40. Each intent has a canonical question, a
category, `must_abstain`, the derivations it reads, and at least five
paraphrases. The paraphrases are development fixtures, not tests. They exist
so routing is never built on exact strings.

`config/intents.yaml` does not hold the questions. It holds the routing
settings ([8.5](#85-configuration)) and the answer categories ([7.3](#73-answer-categories)).

### 7.2 The new intents

| Category | Ids | Questions |
|---|---|---|
| Retrieval | Q21 to Q25 | What happened at checkpoint_4 on 28 July · Which checkpoints are in zone checkpoint_3 · Sensor readings at checkpoint_1 · Operator notes at checkpoint_1 · Findings at checkpoint_4 |
| Aggregation | Q26 to Q31 | How many checkpoints failed · Sensor alerts by severity · Proportion of images that resolved · Zone with the highest average temperature · Findings by feature · Checkpoint with the highest temperature |
| Coverage | Q32 to Q35 | Checkpoints with no evidence · Thermal coverage · What could not be read · Do the declared counts match |
| Comparison | Q36 to Q39 | New findings since the previous run · Findings in both runs · How coverage changed · Sensor blocks available last run but not this one |
| Status | Q40, Q41 | Overall result of the latest run · Findings with no configured action |
| Corpus | Q42, Q43 | When was the route last run · What questions can I ask |
| Must abstain | Q44 to Q49 | Is it safe to operate · Is it NFPA 70B compliant · What caused it · When will it need replacing · Is 2.9 g normal · Who was responsible |

"What questions can I ask" (Q43) is the **catalogue** route. Code writes
that answer from `config/intent_catalogue.yaml`. The model never writes it,
so it only offers what is listed there. Every example in the catalogue must
be a question B-1 can answer today.

### 7.3 Answer categories

`categories` in `config/intents.yaml` maps each route to the category written
to `output.intent` and the log:

| Route | Category |
|---|---|
| catalogue | `corpus` |
| derivation: threshold_compare, condition_count, proportion, group_mean, group_max, rank_top_n, sensor_alert_count | `aggregation` |
| derivation: run_set_difference | `comparison` |
| derivation: run_date_range | `corpus` |
| derivation: evidence_count | `evidence` |
| derivation: count_check | `coverage` |
| derivation: records | `retrieval` |
| lookup: zones, checkpoints, checkpoint_readings | `retrieval` |
| lookup: evidence | `evidence` |

- A derivation is looked up by its **type**: `group_mean_temperature_c` → `group_mean`.
- When the model picks more than one, the first pick's category is used.
- An abstained answer is `abstain_<reason>`: `causation`, `prediction`,
  `determination` or `not_captured`, as the router gave it. It is plain
  `abstain` when code abstained and no router gave a reason.
- An unroutable question has `intent: null`.
- A route with no category is a `ConfigError`, so a new derivation type
  cannot be added without one.

### 7.4 Results

Last full run: `--use both`, tenant `tenant_facility` (7 runs in scope),
canonical questions only. Kept in `output/milestone3/all_answers.txt`.

| Group | Passed |
|---|---|
| Q01 to Q20 | 20 of 20 |
| Q21 to Q30 | 9 of 10  |
| Q31 to Q40 | 9 of 10 |
| Q41 to Q49 | 9 of 9 |
| **Total** | **47 of 49** |

The two that still fail. Both are abstained when they should be answered:

| Id | Question | What happens |
|---|---|---|
| Q28 | What proportion of referenced images resolved? | First step goes ahead (derivation, 0.79). Code then abstains, intent `abstain`. D10 stores the counts of images that resolve and do not, but no share. Not yet confirmed whether that is the cause |
| Q38 | How did coverage change between the last two runs? | First step goes ahead (0.87). The model's derivation router abstains as `not_captured`. No derivation compares coverage between two runs |

Also worth knowing: Q39 passes but as `DEGRADED_TEMPLATE_ONLY`. The prose
writer's lead-in did not pass, so the facts were shown on their own. That
counts as answered.

That run had `second_step_check` on (the answers carry
`second_step_confidence`). It is off in `config/intents.yaml` now.

---

## 8. The confidence router

### 8.1 What it is for

The brief's biggest risk: a router that always picks its best match will
answer a question nobody asked, with real citations attached to the wrong
thing. So a weak match must be **unroutable**, not a weak answer.

The model (B-1's retrieval router) is good at picking a route, but it gives
no confidence figure. The semantic router gives one. So the two work
together: **the model picks, the semantic router confirms.**

It is built with [`semantic-router`](https://github.com/aurelio-labs/semantic-router)
and a local FastEmbed encoder (`BAAI/bge-small-en-v1.5`). No API key, no
model call. The code is in `src/router/`. It imports nothing from the rest
of the project except the question set files.

### 8.2 The two steps

| Router | Routes | Saved as |
|---|---|---|
| First step | `lookup`, `derivation`, `abstain` | `src/router/first_step_router.yaml` |
| Second step, derivation | one per derivation **type** (`threshold_compare`, `condition_count`, `proportion`, `group_mean`, `group_max`, `rank_top_n`, `run_set_difference`, `run_date_range`, `sensor_alert_count`, `evidence_count`, `count_check`) plus `records` | `src/router/second_step_derivation.yaml` |
| Second step, lookup | `zones`, `checkpoints`, `checkpoint_readings`, `evidence` | `src/router/second_step_lookup.yaml` |

`catalogue` has no semantic route. A catalogue question goes ahead without a
check, because code writes that answer from the catalogue, not the records.

The second-step derivation router checks the type only. The model picks
`group_mean_temperature_c`; the router checks `group_mean`. Which reading is
meant is not the router's job.

### 8.3 How it is trained

The routers learn from the 49 intents. Each question's label (which first
step, which second step) is in `src/router/question_set_labels.yaml`. Each
label says whether it came from the question set or a model. Edit any label
you disagree with, then refit.

Each question's wordings are split three ways, so no wording is in two sets:

| Wordings | Used for |
|---|---|
| The question and its first 2 paraphrases | Route utterances: what the router matches against |
| The next paraphrase | Calibration: turns raw scores into probabilities |
| The rest | Tests: seen by neither |

Catalogue questions are left out. On top of that, each router file has
hand-written utterances, calibration and test questions. These are used only
for a route the question sets give no question to. Hand-written off-topic
questions ("what's the capital of france", "write me a poem") are always
added to calibration, so the router learns what a low score looks like.

**Names are swapped out first.** `normalise()` replaces checkpoint and zone
names with `<checkpoint-id>` and `<zone-id>`, so "was a3_back completed" is
matched as "was <checkpoint-id> completed". It knows these shapes only:

- checkpoints: `checkpoint_4`, `checkpoint 4`, `cp4`, and the racks route's
  `a3_back`, `c5_front`, `ups_front`
- zones: the racks route's `rowA_back`, `rowC_front`

Names like `home` or `ac_1` are both a zone and a checkpoint, so they are
left as typed.

**Calibration.** `fit()` fits a Platt curve per router. After that a route's
confidence is a probability between 0 and 1. Each route is scored on its
own, so the confidences of all routes do not add up to 1.

Running a router file without a question prints a test table before and
after the fit, then saves the router.

### 8.4 How the check works at question time

In `query.agreed_route`:

1. The model's retrieval router picks the first step.
2. The question is normalised and scored against every first-step route.
   The printout shows the router's own best route and all scores, but only
   the score of the **model's** pick matters.
3. If that score is below the pick's minimum → unroutable.
4. For lookup and derivation, the model's next router picks tables or
   derivations.
5. If `second_step_check` is on, every pick is scored by the second-step
   router. If **any** pick is below its minimum → unroutable.
6. If the model's next router abstains, there is nothing to check. B-1
   abstains as usual.

Example of what it prints (scores illustrative):

```
model router:    lookup  (asks which zone a named checkpoint is in)
semantic router: best lookup 0.91; lookup 0.91, derivation 0.07, abstain 0.03
going ahead: lookup has confidence 0.91 (minimum 0.25)
model lookup router: checkpoints
```

### 8.5 Configuration

All in the `routing` block of `config/intents.yaml`:

```yaml
routing:
  min_confidence: 0.1          # TUNABLE. Used by any route with no minimum of its own
  second_step_check: false     # true: the second-step router must back each pick
  first_step:                  # a route's own minimum
    lookup: 0.25
    derivation: 0.25
    abstain: 0.25
  second_step:
    derivation:
      threshold_compare: 0.25
      # ... every derivation type and records
    lookup:
      zones: 0.25
      # ... every lookup table
```

- Each minimum must be above 0 and at most 1.
- A route not listed uses `min_confidence`.
- A route or router name that does not exist is a `ConfigError`. A typo is
  never quietly given the default.
- `second_step_check: false` means the model's second-step picks go ahead
  unchecked. The second-step router files are not needed then. Nothing about
  the check is printed, and the answer has no `second_step_confidence` key.
  The first-step check is not affected.
- The brief's default for `min_confidence` is 0.6. Today every route has its
  own minimum of 0.25. See [Open items](#15-open-items).
- `THRESHOLD = 0.6` inside the router files is only used when you run a
  router file by hand. `agreed_route` does not use it. It reads every score
  and applies the minimums above.

### 8.6 When to refit

The saved router files are snapshots. Refit them ([section 2](#refit-the-routers))
after changing any of:

- `config/b1_question_set.yaml` or `config/b1_question_set_extension.yaml`
- `src/router/question_set_labels.yaml`
- the hand-written lists in `first_step_router.py` or `second_step_router.py`
- the derivation types or lookup tables (a route that is missing from a saved
  router file stops `agreed_route` with a message telling you to refit)

`first_step_router.py` points to `router.label_question_set` for writing the
labels. That script is not in the repo. Edit `question_set_labels.yaml` by
hand.

---

## 9. New and extended derivations

Milestone 2 had eight derivation types. There are now **eleven**, set in
`config/derivations.yaml`. B-1's SQL writer has one prompt per type in
`src/agents/routes/`.

| Id | Instance | New or changed | What it gives |
|---|---|---|---|
| D1 | `threshold_compare_vibration_rms_g` | New instance | Checkpoints over a vibration limit of 0.5 g, per run. (The temperature one was renamed `threshold_compare_temperature`.) |
| D2 | `condition_count_finding_status` | New instance (by value) | Findings counted by status. `logged`, `needs_review`, `abstained` always listed, even at 0. Any other status listed as written |
| D2 | `condition_count_finding_feature` | New instance (by value) | Findings counted by feature. Every feature is its own row, as recorded. Similar ones are never merged |
| D7 | `run_set_difference` | Extended | Was: checkpoints only in the previous run, only in the latest, in both. Now also **findings** (matched by id without the run prefix) and **sensor blocks** (available at a checkpoint when its reading passes the exclusion rule), with why a block was not available |
| D9 | `sensor_alert_count` | New type | Per run: how many sensor alerts, and how many of each severity (`warning`, `critical` always listed) |
| D10 | `evidence_count` | New type | Per run and per checkpoint: how many images, how many thermal or RGB (by file name), how many resolve to a readable file and how many do not |
| D11 | `count_check` | New type | Per run: whether the counts the robot declared match the run's own lists. Seven checkpoint and finding counts, and each event's evidence count against its visit |

D1 to D6 and D9 to D11 run once per run, oldest first, and each output
carries its `run_id`. D7 and D8 compare runs and give one output.

All of them hold derived values only: counts and ids, not copies of record
items. A run with nothing to count
gets a count of 0, not an error. When the model's query returns no rows,
code runs the type's `SUMMARY_SQL`, so "none" is stated as a stored count.

The intents that use them: Q27 (D9), Q28 and Q33 (D10), Q35 (D11), Q30 (D2
by feature), Q36, Q37 and Q39 (D7).

---

## 10. "Could not read"

### 10.1 What it is

Sometimes the robot reached something, captured it, and still could not read
it. That is not a gap (the evidence is there) and not a finding (nothing was
called). It is its own category.

```json
"could_not_read": [
  {
    "item_id": "checkpoint_4",
    "subject": "gauge",
    "reason": "DETECTOR_ABSTAINED",
    "detail": "the finding's description",
    "capture_id": "a4_front_rgb",
    "attempted_at": "2026-07-28T14:46:03-07:00"
  }
]
```

| Field | Comes from |
|---|---|
| `item_id` | The finding's `checkpoint_id`. `null` when it names none |
| `subject` | The finding's `feature` |
| `reason` | The reason code from config, exactly as written there |
| `detail` | The finding's `description`, or "no description recorded" |
| `capture_id` | The evidence image's file name without its extension |
| `attempted_at` | The finding's timestamp |

The code is in `src/common/readability.py`, not `src/query`, because the
report uses it and nothing may import from `src/query`.

### 10.2 The reason codes and the placeholders

The brief gives six reason codes. All six are kept in `config/report.yaml`
under `could_not_read.reasons`. Each one lists the finding statuses that
produce it:

| Code | Meaning | Produced by | State |
|---|---|---|---|
| `DETECTOR_ABSTAINED` | A detector declined to call it | `findings[].status == "abstained"` | **Live** |
| `GLARE` | Specular reflection prevented a reading | nothing yet | Placeholder |
| `BLUR` | Focus below the readable threshold | nothing yet | Placeholder |
| `OBSTRUCTED` | The subject was physically blocked | nothing yet | Placeholder |
| `OUT_OF_FRAME` | The subject was not fully captured | nothing yet | Placeholder |
| `ILLEGIBLE` | Captured clearly, could not be parsed | nothing yet | Placeholder |

The five placeholders have `finding_status: []`. They stay in the closed set
so the set is complete. When the robot starts writing a signal for one of
them, add the status to its list. No code change is needed.

A reason code is used exactly as config writes it. A new code added to config
is reported as written, never folded into a known one.

### 10.3 Assumptions

These follow the client's ruling of 2026-10-29:

- **It is derived, not stored.** The run records have no could_not_read
  field, and the schema is not changed.
- **The only producer today is an abstained finding.** The brief assumed
  gauge and label reading signals (glare, blur and so on). Those are not
  built on the robot yet.
- **`needs_review` is not could_not_read.** It means nobody has looked yet,
  not that it could not be read.
- **Empty or undecodable images stay `MISSING_IMAGE` gaps.** They are
  missing evidence, not unreadable evidence (accepted Milestone 1 tests
  TA-13 and TA-14).
- **Only run-level findings are read.** Checkpoint detections are skipped. If
  they carry the same finding, reading both would count one abstention
  twice. Detections are empty in every run today anyway (the vision detector
  is not running on the robot).
- **Very few entries is the correct output.** On this corpus that may be one
  entry, or none.
- Two reasons claiming the same status is a `ConfigError`.

### 10.4 Where it appears

| Place | How |
|---|---|
| Report | Its own section (`src/report/templates/_could_not_read.html.j2`), not folded into gaps |
| Manifest | Its own `could_not_read` array |
| Row sign-off | One of the five shortfall categories ([section 11](#11-row-sign-off-section-6)) |
| Answers | Q34 "What could not be read on the most recent run?" is answered, but see below |

Q34 today is answered through the `records` route. It lists the sensor
readings that were left out and why (missed checkpoint, offline sensor). It
does not yet read the could_not_read entries above. See
[Open items](#15-open-items).

---

## 11. Row sign-off (section 6)

### 11.1 What it is

A row of racks is signed off for a run with a confidence figure. Code works
it out from coverage. A model never states it.

```
confidence = readable_subjects / expected_subjects
```

**Confidence is coverage, not condition.** It answers "how much of this row
do we actually know about", not "how healthy is it". A row where everything
was read and everything failed has confidence 1.0. The report says this in
words, so nobody reads 0.83 as a health score.

The code is in `src/common/signoff.py` (for the same reason as could_not_read).

### 11.2 Output

The shape, with illustrative values:

```json
{
  "row_id": "row_a",
  "run_id": "20260730_145833-sis_racks_checkpoint_route",
  "signed_off_at": "2026-10-31T09:00:00Z",
  "confidence": 0.84,
  "confidence_formatted": "84%",
  "expected_subjects": 13,
  "readable_subjects": 11,
  "shortfall": {
    "missed_checkpoints": 1,
    "unresolved_captures": 1,
    "unscoreable_captures": 0,
    "could_not_read": 0,
    "abstained": 0
  },
  "below_threshold": true,
  "threshold": 0.90
}
```

The manifest's `row_signoff` entries carry only these keys. The list of what
was not covered is used only for the report's wording.

Below the threshold (0.90, TUNABLE) the row is reported as **not signed
off**, and the report says what was not covered and why.

### 11.3 How each subject is counted

One subject is one checkpoint. Each checkpoint is counted in exactly one
place: the first of these that applies.

| Order | Category | When |
|---|---|---|
| 1 | `missed_checkpoints` | Not in the run, or its status is not `COMPLETED` (so `PENDING` and `IN_PROGRESS` count as missed) |
| 2 | `unresolved_captures` | No images recorded, or `observed = no_evidence`, or none of its images is a readable file |
| 3 | `unscoreable_captures` | Its images are fine, but it has no `PASS`, `FAIL` or `WARN` verdict |
| 4 | `could_not_read` | A could_not_read entry names it |
| 5 | `abstained` | One of its own detections abstained |
| – | readable | None of the above |

Because each checkpoint lands in one place only, `expected - readable` always
equals the shortfall total. The brief's TQ-16 checks that.

### 11.4 Where the rows come from

Rows are set per facility, one file each, in `config/signoff/<facility_id>.yaml`:

```yaml
facility_id: sis_racks_checkpoint_route

rows:
  row_a:
    checkpoints: [a1_front, a2_front, ..., a6_back]
    expected_subjects: 13        # TUNABLE, PLACEHOLDER
  row_b:
    checkpoints: [b1_front, ..., ups_front, b1_back, ..., ups_back]
    expected_subjects: 14        # TUNABLE, PLACEHOLDER
  row_c:
    checkpoints: [c1_front, ..., c7_back]
    expected_subjects: 12        # TUNABLE, PLACEHOLDER
```

The settings (threshold, `rows_dir`, the statuses each category reads) are
the `signoff` block of `config/report.yaml`. The brief lists a separate
`config/signoff.yaml`. It was removed so the threshold has one home and is
covered by `config_hash`.

The file is refused when:

- its `facility_id` does not match the file name
- `expected_subjects` is not the number of checkpoints listed
- a checkpoint is listed in two rows

A facility with no file is not signed off.

### 11.5 Placeholders and assumptions

- **`expected_subjects` are placeholders.** Each is the row's checkpoint count
  until the client sends the real counts. That is also why it must equal the
  number listed: any other number would leave subjects no category accounts
  for.
- Only two facilities have row files: `sis_racks_checkpoint_route` and
  `sis_facility_checkpoint_route`.
- **Racks rows follow the record's `rack.row` field** (rows A, B, C). The UPS
  is in row B, as the record files it. `ac_1`, `ac_2` and `dock_standoff` are
  in no row.
- **`sis_facility` has no racks.** So `checkpoint_1` to `checkpoint_7` are one
  row, and the dock (`home_docking_station`) is left out.
- **`ups_back` is only on runs from 2026-08-18 on.** On earlier runs it counts
  as missed, so row B on those runs is at most 13/14.
- **could_not_read is checked before abstained.** An abstained run-level
  finding is already a could_not_read entry. Counting it again would break the
  arithmetic.
- **`unscoreable` and `abstained` are always 0 on today's data.** Every
  completed checkpoint has a verdict, and detections are empty in every run.
  The rules are there so a future run is not counted as readable when it is
  not.
- **Confidence is rounded down** to two places, so the figure shown is never
  higher than the real one. The threshold is checked against the figure
  shown, so the verdict always matches what the reader sees.
- `signed_off_at` is the report's `generated_at`, so the PDF, the manifest and
  the sign-off share one time.
- A checkpoint visited twice in one run is judged by its last visit.

---

## 12. How to word a question

The agent abstains when it cannot work out what is being asked. A specific
question gets an answer. A vague one gets an abstention or is unroutable.

### 12.1 Name three things

1. **The run**: which run or runs it is about.
2. **The figure**: what is being worked out or looked up.
3. **The item**: checkpoint, zone, finding, sensor alert, image, or the run itself.

> "In the most recent run, which checkpoints went over the temperature threshold?"
>
> run: "the most recent run" · figure: "over the temperature threshold" (D1) · item: "checkpoints"

Using the figure's own word works best: threshold, mean, average, peak, top
five, percentage, by status, by severity. Loose wording is fine as long as it
is not vague. "Went over the temperature limit" works. "Was it too hot" does
not.

### 12.2 Naming the run

| You mean | Say |
|---|---|
| Every run | "across all runs", "in each run", or name no run |
| First, second, ... | "the first run", "the first and third runs" |
| Latest | "the latest run", "the most recent run" |
| The one before | "the run before the latest", "the last two runs" |
| One day | "the runs on 31 August", "on 19/8/2026" |
| A span | "the runs from 14 to 21 August 2026", "the runs in August 2026" |

- Naming no run means every run. Say "the latest run" if that is what you mean.
- A date means every run that started that day. There may be more than one.
- Dates with slashes are day first: 3/4/2026 is 3 April.
- A date with no run on it gets an abstention, not an empty answer.
- "Every run" means every run in **your** scope. Use `--from`/`--to` or
  `--facility` to narrow the scope itself.

### 12.3 Naming the item

- Names can be written loosely: "row A back" finds `rowA_back`.
- A zone and a checkpoint can share a name. Say "at <name>" for the
  checkpoint, "in the <name> zone" for the zone.
- A zone or checkpoint not in your records gets an abstention.
- The router recognises checkpoint names like `checkpoint_4`, `cp4`, `a3_back`
  and zone names like `rowA_back`. Other names still work, but the router
  scores them as typed.

### 12.4 One thing per question

Avoid "and". A joined question makes the agent fetch from several tables
when it does not need to. It is slower and more likely to go wrong.

| Avoid | Better |
|---|---|
| What was a3_back's status and its temperature in the latest run? | Was a3_back completed in the latest run? · What was the temperature at a3_back in the latest run? |
| Which checkpoints failed and which were missed? | Which checkpoints failed in the latest run? · Which checkpoints were missed in the latest run? |

### 12.5 What can be asked

| Kind | Examples |
|---|---|
| D1 threshold | "Which checkpoints exceeded the temperature threshold in the latest run?" · "Which checkpoints went over the vibration limit?" |
| D2 counts | "How many checkpoints failed in the latest run?" · "Breakdown of findings by status" · "How many findings by feature?" |
| D3 share | "What percentage of checkpoints were completed in the latest run?" |
| D4 mean per zone | "Mean vibration per zone in the latest run" (vibration, temperature, PM2.5) |
| D5 max per zone | "Which zone had the highest peak temperature?" |
| D6 top five | "The five highest vibration readings in the latest run" |
| D7 last two runs | "Which checkpoints are new in the latest run?" · "Which findings are new since the previous run?" · "Which sensor blocks were available last run but not this one?" |
| D8 dates | "Over what period were the runs taken?" · "When was this route last run?" |
| D9 alerts | "How many sensor alerts were logged, by severity?" |
| D10 images | "How many images were captured at each checkpoint?" · "What thermal coverage exists on the latest run?" |
| D11 declared counts | "Do the declared counts match the checkpoints in the record?" |
| Records | "Which findings need review?" · "Which checkpoints were missed and why?" · "How did the latest run end?" |
| Lookup | "Was a6_back completed?" · "Which zone is a3_back in?" · "What was the temperature at a3_back?" · "Which images were taken at a3_back?" |
| Catalogue | "What questions can I ask about these records?" |

Where the line falls:

- "The temperature at a3_back" → lookup (one recorded value)
- "Did a3_back go over the temperature limit" → derivation (D1)
- "The highest temperature in rowA_back" → derivation (D5)

### 12.6 What is always refused

- **Why** something happened (`abstain_causation`)
- **What will** happen, or when (`abstain_prediction`)
- **A judgement**: safe, normal, compliant, better, worse (`abstain_determination`)
- **Things the records never hold**: who, manufacturers, costs, how long a
  route has existed (`abstain_not_captured`)
- A figure no derivation keeps: a failure rate, an overall mean, the lowest
  reading, gaps between runs, runs per week
- Comparing runs other than the last two
- Anything about another tenant's facilities. It is not refused as such:
  those records are simply not there, so the answer covers your own records
  only.

### 12.7 Checklist

- [ ] Does it name the run?
- [ ] Does it name the figure or the value?
- [ ] Does it name the item?
- [ ] Does it ask for one thing, with no "and"?
- [ ] Is it in the table in 12.5?

---

## 13. Files in this milestone

### `src/query/`

| File | What it does |
|---|---|
| `scope.py` | `QueryContext` and `resolve`. The only module that takes a `tenant_id` or loads records |
| `ask.py` | `ask(question, context, settings)`. Scope, derive, answer, log. Model only, no confidence check. Also `load_settings`, `answer_intent`, `log_answer` |
| `agreed_route.py` | The same steps, with the confidence router checking the model's first and second step. Unroutable answers. The customer path |
| `main.py` | Command line for `ask` |
| `query_log.py` | `append_entry`. Writes one JSON line. Never reads |
| `run_question_set.py` | Asks every question (and paraphrase) by `router`, `model` or `both` |
| `run_milestone3.py` | Asks each canonical question once and keeps full answers and PDFs |
| `intents.py` | Docstring only. Routing lives in `agreed_route.py` and `src/router/` |
| `readability.py` | Docstring only. The code is `src/common/readability.py` |
| `signoff.py` | Docstring only. The code is `src/common/signoff.py` |

`src/query` imports from `common`, `agents` and `router`. Nothing imports
from `src/query`.

### `src/router/`

| File | What it does |
|---|---|
| `first_step_router.py` | Builds, fits, tests and saves the first-step router. `normalise()` |
| `second_step_router.py` | The same for the derivation and lookup routers |
| `first_step_router.yaml` | Saved first-step router: utterances, encoder, Platt parameters |
| `second_step_derivation.yaml`, `second_step_lookup.yaml` | Saved second-step routers |
| `question_set_labels.yaml` | First- and second-step label for each of the 49 intents |

### `src/common/`

| File | What it does |
|---|---|
| `readability.py` | `could_not_read(record, config)` |
| `signoff.py` | `facility_rows`, `sign_off`, `RowSignOff` |

### `src/agents/` (changed for Milestone 3)

| File | Change |
|---|---|
| `derivation.py`, `database_derivation.py` | D9, D10, D11; D7 findings and sensor blocks; D2 by feature |
| `routes/sensor_alert_count.py`, `routes/evidence_count.py`, `routes/count_check.py` | New SQL writer prompts |
| `routes/run_set_difference.py` | Findings and sensor block tables, readable column labels |
| `b1_catalogue.py` | Checks and writes the "what can I ask" answer |

### `config/`

| File | What it holds |
|---|---|
| `tenants.yaml` | Tenant → facilities, the tenant data folder and record pattern |
| `intents.yaml` | Routing minimums, `second_step_check`, answer categories |
| `b1_question_set.yaml`, `b1_question_set_extension.yaml` | The 49 intents and their paraphrases |
| `intent_catalogue.yaml` | What "what can I ask" offers |
| `derivations.yaml` | The eleven derivation types and their instances |
| `report.yaml` | `could_not_read` reasons and the `signoff` block |
| `signoff/<facility_id>.yaml` | Each facility's rows |
| `agent_path.yaml` | `query_logs` folder, databases, answer PDF folder |

---

## 14. Configuration reference

| Setting | File | Value now | Note |
|---|---|---|---|
| `routing.min_confidence` | `intents.yaml` | 0.1 | TUNABLE. Brief default 0.6. Only used by a route with no own minimum |
| `routing.first_step.*` | `intents.yaml` | 0.25 each | |
| `routing.second_step.*` | `intents.yaml` | 0.25 each | |
| `routing.second_step_check` | `intents.yaml` | `false` | |
| `signoff.threshold` | `report.yaml` | 0.90 | TUNABLE |
| `rows.*.expected_subjects` | `signoff/*.yaml` | checkpoint count | PLACEHOLDER |
| `could_not_read.reasons.*.finding_status` | `report.yaml` | only `DETECTOR_ABSTAINED: [abstained]` | Five placeholders |
| `data_dir`, `record_patterns` | `tenants.yaml` | `data_tenant`, `{facility_id}/*/run.json` | |
| `agent.query_logs` | `agent_path.yaml` | `logs` | |

Config is read once at the entry point and passed down. No module reads
config when it is imported.

---

## 15. Open items

Things still to do or decide before the milestone is closed:

1. **Q28 and Q38 fail** ([7.4](#74-results)).
2. **TQ-01 to TQ-22** are not written yet. `tests/` holds only the Milestone 1
   acceptance tests.
3. **The 60-question transcript** (40 canonical + 20 adversarial) is not
   built. `run_milestone3` covers the 49 canonical questions; the adversarial
   20 are not written.
4. **Not every question is logged.** In `agreed_route`, when the model's
   first or second router gives no valid reply, it prints `stopped:` and
   returns without writing a log line.
5. **`config_hash` does not cover `config/intents.yaml`** (the intent set
   version and routing minimums) or the row files in `config/signoff/`. It
   does cover `tenants.yaml` and `report.yaml`.
6. **Q34 does not read could_not_read entries.** It answers from the records
   route (excluded readings). An answer built from the could_not_read class
   is not wired into B-1 yet.
7. **TUNABLE changes need a line in `decisions.md`**: `min_confidence` (0.6
   → 0.1) and the per-route minimums (0.25) with a one-line reason each.
8. **`query.main` gives no `routing_confidence`.** Only `agreed_route` does.
9. **Client input still needed:** the real `expected_subjects` per row, and
    which robot signals should feed the five placeholder could_not_read
    reasons.
