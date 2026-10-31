# Decisions

1. Add another ENUM type for INCORRECT_DATATYPE

2. Hatchling is used for the backend 

3. To make it vendor agnostic file names are parsed differently the assumption here is that image names are like this

a2_front/a2_front_rgb.jpg, that implies that naming convention is as follows <checkpointname><image type (rgb or thermal)> 
ac_2_h000_thermal.jpg implies that naming convention is <checkpointname><direction or other name><image type (rgb or thermal)>

so splitting is done on this rule on that we have checkpointname,direction,thermal or rgb

4. I have added a style config. Where we can select the style to be used, this can be helpful for different vendors where if you need a specific style for a vendor you can just tweak the config file and select the required style

5. For the acceptance I have changed the run.json accordingly to mimic the test cases. If you want you can look into them and run them via commands in README.md

6. The code is modular but not optmized , if needed later that can be done


7. Row sign-off (Milestone 3). The calculation is in src/common/signoff.py, not src/query, because the report uses it and nothing may import from src/query. This is the same as could_not_read.

- The sign-off settings are the signoff block of config/report.yaml: the threshold, rows_dir, and each shortfall category with its meaning and the statuses it reads. config/signoff.yaml, which section 7 of the brief lists, was removed, so the threshold has one home and is covered by config_hash.
- Rows are defined per facility, one file each in config/signoff/<facility_id>.yaml. Each row lists its checkpoints and its expected_subjects. A facility with no file is not signed off. For now only sis_racks_checkpoint_route and sis_facility_checkpoint_route have files.
- One subject is one checkpoint. expected_subjects must equal the number of checkpoints listed, or the file is refused. Any other number would leave subjects that no shortfall category accounts for.
- expected_subjects are placeholders (the checkpoint count of each row) until the client sends the real counts.
- The racks rows follow the rack.row field in the record (rows A, B, C). The UPS is in row B, as the record files it. ac_1, ac_2 and dock_standoff are in no row. sis_facility has no racks, so checkpoint_1 to checkpoint_7 are one row and the dock is left out.
- ups_back is only on runs from 2026-08-18 onward. On earlier runs it counts as a missed checkpoint, so row B on those runs is at most 13/14.
- Each checkpoint is counted in exactly one place, the first that applies: missed (not in the run, or status is not COMPLETED, so PENDING and IN_PROGRESS count as missed), unresolved (no image is a readable file, or observed = no_evidence), unscoreable (no PASS, FAIL or WARN verdict), could_not_read, abstained (one of its own detections abstained), otherwise readable. So expected - readable always equals the shortfall total.
- could_not_read is checked before abstained. An abstained run-level finding is already a could_not_read entry, and counting it again would break the arithmetic.
- On the current data unscoreable and abstained are always 0: every completed checkpoint has a verdict, and detections are empty in every run. The rules are there so that a future run is not counted as readable when it is not.
- Confidence is rounded down to two places, so the figure shown is never higher than the real one. The threshold is checked against the figure shown, so the verdict always matches what the reader sees.
- signed_off_at is the report's generated_at, so the PDF, the manifest and the sign-off share one time.
- The manifest's row_signoff entries carry only the brief's keys. The list of what was not covered is only used for the report's wording.
- A checkpoint visited twice in one run is judged by its last visit.
- Open: config_hash covers report.yaml, so it covers the signoff block, but not the per-facility row files. A change to a row file does not change it.

8. Second-step routing check (Milestone 3). config/intents.yaml has routing.second_step_check. true: the second-step semantic router (src/router/second_step_derivation.yaml and second_step_lookup.yaml) must back each of the model's derivation or lookup picks at or above its minimum, or the question is unroutable. false: the picks go ahead unchecked, and nothing about the check is printed or put in the answer (no second_step_confidence key). Set to false: the check is off. The first-step check and min_confidence are not affected.
