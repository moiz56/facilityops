# Decisions

1. Hatchling used to build backend 

2. branding.yaml is a standalone file but the config/report.yaml in the document contains a branding section, for now I have put both in the config.yaml making branding.yaml empty (can be discussed)

3. common/schema.py implemented, some helper classes were made like Pose, Point2D to make it easier to structure data

4. common/loader.py implemented. Nothing is coerced: a wrong type is refused, left unset, and recorded as a FieldAnomaly, so a confidence written as "0.94" never reaches the page as a measurement (TA-27). Only an unusable record raises - unreadable file, invalid JSON, non-object root, or no run_id, since 4.1 names the output files from it. Everything else loads with the problem recorded.

5. FieldAnomaly.item_id is a checkpoint_id where the field is a checkpoint's, __run__ for a run-level field, or a locator like sensor_alerts[3] for an array entry with no id. Alerts and samples never borrow their nearest_checkpoint_id: that is proximity, not attribution (2.5).

6. confidence 0.0 resolves to None in the loader, per 2.9. The difference between an absent key and a recorded 0.0 is not kept, because 2.9 gives them the same meaning.

7. Keys section 2 does not describe are logged at debug and counted in the run summary, not warned about. An extra key loses nothing; a refused value does.

8. report/cli.py runs the stages of 5.2 as plain calls in `run_pipeline()`, in order, not through a stage registry. It returns an `Artifacts` object holding what the stages produced - currently the record, the resolved images per checkpoint, and the eight-cell grid per checkpoint - so a later stage adds a field rather than changing the call. A dict of stage functions, a frozen State threaded between them, and the guard that checked a stage had not run out of order were all removed: for a handful of stages they cost more to read than they explain. The `--list-stages` and `--stop-after` flags went with them; neither is asked for anywhere in the brief, and `-v` covers the debugging they were for.

9. `--dump-record` writes the parsed record as JSON, to a file or to stdout. Not a deliverable - the outputs are the PDF and the manifest (4.1) - but it makes the loader's reading of a file inspectable, and gives an evidence artifact for the parsing tests rather than a claim (deliverable 11). Datetimes and paths are written as text, so the dump reads as what the loader made of the record, not as the record.

10. `checkpoints[].evidence_images` is the only source for the eight-cell evidence grid (3.2) and for the manifest's `images_referenced` and `images_rendered` (4.2). The record carries two other recorded robot paths - `checkpoints[].annotated_images` (2.2) and `findings[].evidence_image` (2.4) - and both are empty or null in all seven supplied runs. Both are still loaded, and both still rewrite through `resolve_evidence`, because 11 requires an optional field absent from every supplied record to be supported anyway; neither enters a direction cell and neither is counted. A cell's contents are fixed by 3.2 as an RGB image with its thermal counterpart beneath it, so a third modality has nowhere to sit in one. 4.2's example manifest cannot settle the count by itself: it reports `images_referenced: 87` for a run where the other two fields are empty, so 87 is equally consistent with either reading. Section 9 settles it, describing the reference run as "8 checkpoints, 87 evidence images" and naming the field. Where those two fields render, if anywhere, is question 6.

11. `resolve_evidence` and `build_direction_grid` take the resolved config as a trailing parameter with a default. Both need 5.7 values their signatures in 5.3 do not carry - `resolve_evidence` the `evidence.thermal_suffix`, `build_direction_grid` the `evidence.directions` - and hard-coding those would leave two config keys decorative. The default keeps 5.3's call shape working unchanged, since 0 says code the client has already written consumes these signatures, while the pipeline passes the config it actually loaded. `evidence.grid_columns` is needed by neither: 3.2's "four across, two rows" is a property of the template, not of an eight-cell list.

12. An evidence path is joined exactly as recorded, with no case folding and no fallback search (TA-21). A case-insensitive search could resolve to a different file from the one the robot recorded, and an honest gap is better than a confident wrong answer. No path is shell-quoted, escaped or split on whitespace anywhere, because the recorded directory names contain spaces.

13. The direction token in a filename is matched without regard to case and stored uppercase, so `checkpoint_1_n.jpg` renders in the N cell instead of being dropped. This cannot select the wrong file: the file has already resolved, and the token only decides which of the eight cells it occupies. The thermal suffix is matched as written.

14. Provisional, pending question 7. MISSING_THERMAL fires for any direction carrying a referenced RGB with no thermal counterpart, whether or not that RGB resolves, so a zero-byte RGB reports both MISSING_IMAGE and MISSING_THERMAL for one direction. Read the other way - 4.3's "a direction has an RGB image" meaning a usable one - a single broken file would suppress a second, unrelated gap, and a thermal that was never captured would go unreported. The detection site in report/gaps.py carries the same note.

15. `ResolvedImage` is declared without `kw_only`, unlike the types in common/schema.py, because 5.3 prints it with a fixed field order and no defaults; positional construction stays available to code written against the brief.

16. A path that does not begin with the configured `path_prefix` still resolves if it can: the leading separator is dropped and the remainder joined to the root. Refusing outright would turn a recoverable prefix mismatch into a reported missing image.

17. Test evidence trees are built inside pytest's `tmp_path` at test time, not committed. A zero-byte file, a text file wearing a `.jpg` extension, a directory whose name carries spaces and mixed case, and a real JPEG from Pillow are all a few lines each, so TA-18 to TA-21 need no binary fixtures for a clean machine to fetch (TA-08) and each failure case is legible in the test source rather than hidden in a file nobody can open. `.gitignore` therefore keeps excluding `tests/data`, and the two tests that read the supplied reference run skip when `data/` is absent rather than failing.

18. common/paths.py reads config/report.yaml directly, at import, for the three values it needs: `evidence.directions`, `evidence.thermal_suffix` and `paths.path_prefix`. 5.1's layout is decided and adding a module to it is a larger deviation than letting this one read a file; common/ reading a file breaks no rule, since 5.1 forbids common importing from report, not reading data. Nothing reads the report, sensor, sections or branding sections yet, and 5.7's rules for a missing key and an unknown key are not implemented. Neither is the `config_hash` that 4.4 requires of the manifest. All three still need an owner.

# Questions to raise

1. branding.yaml is a standalone file but the config.yaml in the document contains a branding section, for now I have put both in the config.yaml making branding.yaml empty but is this the way to go
2. raise question as to how it is expected the data is fed
3. questions regarding objects , the one shown over there. for now I have given each a deterministic shape but if they are dynamic schema.py must be adjusted

4. a field of the wrong type has no gap type in 4.3, which is a closed set of ten. Confirm these belong in the PDF but not in the manifest gaps array (TA-27 says "handled or reported clearly", not which).

5. 2.5, 2.6 and 2.7 give no optional/required column, only JSON examples. The fields treated as required there are a judgement and would change if a supplied record disagrees.

6. `annotated_images` (2.2) and `findings[].evidence_image` (2.4) are null or empty in all seven supplied runs, and 3.2 does not list either in the per-checkpoint section. Decision 10 keeps both out of the evidence grid and out of the manifest image counts. Whether they should render anywhere else - annotated frames in a labelled block below the grid, a finding's supporting image beside that finding - cannot be checked against any supplied record. Confirm whether a record that populates them is expected.

7. 4.3 defines MISSING_THERMAL as "a direction has an RGB image and no thermal counterpart" and does not say whether a referenced but unusable RGB counts as having one. Decision 14 fires the gap either way, so a zero-byte RGB reports both MISSING_IMAGE and MISSING_THERMAL for the same direction. Confirm which reading is wanted: it changes the gap count in the manifest, and 4.4 tests the manifest against the PDF in both directions.

8. Two entries in one checkpoint's `evidence_images` could parse to the same direction and modality, and 3.2 gives a cell room for one RGB and one thermal. No supplied run contains such a duplicate - checked across all seven - so there is nothing to verify a choice against, and the case can only arrive in a record supplied at delivery. Open: does the cell take the first entry in array order, or the first that resolves and decodes, with the remainder listed below the grid? `build_direction_grid` currently takes the first in array order, as the smaller assumption, pending an answer.

9. 14 says an image beyond the eight known directions is "listed below the grid, labelled", but 5.3 pins `build_direction_grid` to return `list[DirectionCell]`, which has nowhere to carry one. No supplied run has a filename that parses to no direction - checked across all seven. Open: return the extras from a second function in report/images.py, leaving 5.3's signature untouched, or widen the return type to carry both. `build_direction_grid` currently leaves such an image out of the grid altogether, so nothing renders it.
