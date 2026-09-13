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

19. An unusable sensor reading reports SENSOR_UNAVAILABLE and nothing else. No SUBSYSTEM_OFFLINE and no STALE_READING is emitted alongside it, because the whole table then reads "Sensor unavailable" and a gap naming one block inside it would describe something the page never shows. 4.4 requires every manifest gap to appear in the PDF and every PDF gap to appear in the manifest, and tests both directions. Raised as question 10, since an auditor might still want to know which device was dead.

20. `status` must say "connected" for a reading to be trusted, so a reading that never says what its status was is treated as unavailable. `ok` and `sensor_hub_reachable` are read the other way: a failure only when they actually say false. This follows how 4.3 words each one - "status not connected" sets a positive requirement, while "ok false" and "hub unreachable" name a negative condition - and where the brief is silent an honest gap beats printing a number nobody vouched for. No supplied record exercises this: in all seven, every sensor block that exists has all four fields present.

21. A sensor block with no `raw` block emits no SUBSYSTEM_OFFLINE gaps. 4.3 defines that gap as "a raw device flag is false", and an absent flag says nothing, so nothing is disqualified. It is not treated as unavailable either, since 4.3's list for SENSOR_UNAVAILABLE does not include it. No supplied record exercises this: all 52 sensor blocks across the seven runs carry a raw block.

22. A MISSED checkpoint reports NO_EVIDENCE as well as MISSED_CHECKPOINT. 4.3 defines the two by independent conditions and neither says "unless the other applies", 3.6 lists them as separate situations, and 3.2 renders the eight-cell grid for every checkpoint - so a missed one still shows an empty grid and the page carries both facts. Unlike decision 19, suppressing one here would hide something the page does show. In the supplied data this affects 22 checkpoints across three runs.

23. MISSING_IMAGE is one gap per broken image, carrying the reason resolution gave, so the manifest tells an absent file from an empty one from an undecodable one (TA-18, TA-19, TA-20). MISSING_THERMAL is one gap per checkpoint naming the directions, matching 4.2's example detail: eight separate gaps all saying the same thing would bury everything else in the manifest.

24. A status that is neither COMPLETED nor MISSED raises no MISSED_CHECKPOINT gap. Three supplied runs carry `status: PENDING`, which 2.2 does not list; 11 requires an unlisted value to be rendered as written rather than mapped onto a known one, so it is not read as a miss. The same applies to `observed`: `waypoint_reached` and `navigation_failed` both appear in supplied records and neither is `no_evidence`, so neither raises NO_EVIDENCE on its own - though every checkpoint carrying them happens to have an empty evidence_images, which does.

25. The coverage page has seven rows, not six. 2.9 lists six declared counts plus evidence_count and omits `total_completed_checkpoints`, but 3.3's table includes a Completed row and 4.2's `counts` object has a `completed` key in both halves. 3.3 and 4.2 are the specific statements of what the page and the manifest contain, so both are built to seven. Four of the seven supplied runs disagree on exactly that row.

26. Computed counts live in report/derive.py, which 5.3 names as their home, and gaps.py imports them rather than counting for itself. 5.2 runs detect_gaps before derive, but that is the order data flows, not the order modules may import: one implementation of a count is what keeps the coverage page and the manifest gap saying the same number. The same reasoning moved grid building out of missing_thermal_gaps.

27. A count the record never declared raises no COUNT_MISMATCH. There is no claim for the array to disagree with, and the loader has already recorded the field as absent. Comparing an absent count against a computed one would report "declared None", which states nothing an auditor can act on.

28. COUNT_MISMATCH has three sources, and the second is the one TA-23 turns on. The seven coverage rows and the warned-checkpoints contradiction are run-level; the event_log `evidence_count` check is owned by its checkpoint, because TA-25 asks for it "per checkpoint" and 4.4 only says __run__ is used for run-level gaps "such as" COUNT_MISMATCH, not that every one is. In the reference run the Warned row agrees at 0 while seven checkpoints carry sensor warnings, so the rows alone would report nothing.

29. NO_FINDINGS fires for every checkpoint no finding names, with no exception for a missed one. 4.3 defines it as "no findings reference this checkpoint" and adds no condition, and 3.6 has the page say "No findings recorded for this checkpoint" - so the gap and the line on the page match. In the reference run that is seven of eight checkpoints, which is the normal case rather than a fault: it lets a reader tell "nothing was found" from "nobody looked".

30. `checkpoint_gaps` takes the run's findings as a required argument rather than defaulting it to empty. An empty default would report every checkpoint as having no findings whenever a caller forgot to pass them - a wrong answer rather than a missing one, and the kind that reads as real data. A test caught exactly that while this was being built.

31. `detect_gaps` matches 5.3's two-argument shape and takes the grids as an optional third. Given them, nothing is paired twice; without them it pairs the images itself, so the call in 5.3 still works. Per-checkpoint gaps come first in route order, then the run-level ones, which is the order 4.2's example manifest shows.

32. `SUBSYSTEM_FLAGS` moved from report/gaps.py to report/derive.py, which gaps.py imports and re-exports. derive needs the same flag-to-block map to suppress a zone statistic, and gaps.py already imports derive (decision 26), so derive cannot import it back. Both modules reading `sensor.subsystem_flags` separately would work, but one definition is what stops a SUBSYSTEM_OFFLINE gap and a suppressed zone cell from ever naming different devices. No new module for it: 5.1's layout is decided.

33. The zone telemetry table has no particulate column. 2.6 names the three measurements the report shows per zone - temperature, humidity and vibration RMS - and 3.4's table lists exactly those three. 3.4's "particulate is excluded from this table entirely while sps30_ok is false" and 5.3's "excludes particulate from rollups when the governing flag is false" are read as saying where the exclusion lives - derive.py, not a template - rather than authorising a fourth column that appears when the flag is true. A column nobody asked for is what 10 calls a failure of the milestone rather than a bonus. The placeholder zeros are therefore never summarised at all.

34. A zone's statistic is suppressed by the device flags of the checkpoints standing in that zone. 2.6 says a sample's sensor block is a reduced one with no `raw`, so a sample carries no flag of its own and nothing else can say whether its device was working. TA-11 pins the consequence - a checkpoint with `adxl345_ok: false` keeps its vibration figures out of the rollup - and 3.4 groups by zone, so the flag reaches the table through the checkpoint's zone.

35. Where two checkpoints in one zone disagree about a flag, the block is suppressed for the whole zone. 2.2 calls `zone` a grouping key and never promises one checkpoint per zone. A mean mixing a real reading with a placeholder is the confident wrong answer 0 rules out, and there is no way to tell which samples came from which checkpoint: `nearest_checkpoint_id` is proximity, not attribution (2.5), and decision 5 already refuses to read it that way. Raised as question 11.

36. A sample's own `status` is not a trust signal. 2.8's rules name `ok`, `status` and `sensor_hub_reachable` on a checkpoint's sensor block, and 2.6 says a sample's block carries none of them; the `status` beside a sample is a different field. Nothing in the brief excludes a sample by status, so every sample is counted in the zone's sample count and every value it recorded is summarised.

37. `ZoneStat` carries `used` and `suppressed_by` rather than three optional floats. Three different things leave a cell with no numbers - the zone recorded no samples, the device was offline, or samples were taken and none carried that value - and 0 makes telling them apart the acceptance criterion rather than a preference. `suppressed_by` holds the flag name because 3.6 requires the sentence to name the device. A template reads those two fields and the row's sample count and works nothing out for itself, which is 5.4's rule applied to a statistic instead of to a gap.

38. A zone that recorded no samples still gets a row, with a sample count of zero, and samples or alerts that recorded no zone collect in a final row of their own. 3.4 asks for one row per zone and the route is what defines the zones; omitting one because it logged no telemetry, or dropping telemetry because it named no zone, is the silent handling 3.6 forbids. 11 requires an optional field to be supported wherever it is absent.

39. Nothing in derive.py formats. A mean is left unrounded and no figure is turned into text, so how a number is rounded and how an empty cell is worded stay in one place, the templates.

# Questions to raise

1. branding.yaml is a standalone file but the config.yaml in the document contains a branding section, for now I have put both in the config.yaml making branding.yaml empty but is this the way to go
2. raise question as to how it is expected the data is fed
3. questions regarding objects , the one shown over there. for now I have given each a deterministic shape but if they are dynamic schema.py must be adjusted

4. a field of the wrong type has no gap type in 4.3, which is a closed set of ten. for now these show in the PDF but not in the manifest gaps array, TA-27 only says "handled or reported clearly" and not which, so needs confirming

5. 2.5, 2.6 and 2.7 give no optional/required column, only JSON examples. what I have treated as required there is my own reading and would change if a supplied record disagrees

6. annotated_images (2.2) and findings[].evidence_image (2.4) are null or empty in all seven runs and 3.2 does not list either, so for now both stay out of the evidence grid and out of the image counts. is a record that fills them expected, and if so where do they render - annotated frames below the grid, a finding's image next to the finding?

7. 4.3 says MISSING_THERMAL is "a direction has an RGB image and no thermal counterpart" and does not say whether a referenced but unusable RGB counts as having one. for now the gap fires either way, so a zero-byte RGB reports both MISSING_IMAGE and MISSING_THERMAL for one direction. which reading is wanted - it changes the manifest gap count and 4.4 tests the manifest against the PDF both ways

8. two entries in one checkpoint's evidence_images could parse to the same direction and modality, and 3.2 only gives a cell room for one RGB and one thermal. no supplied run has such a duplicate so there is nothing to check a choice against. for now the cell takes the first in array order as the smaller assumption, but should it be the first that actually resolves and decodes, with the rest listed below the grid

9. 14 says an image beyond the eight known directions is "listed below the grid, labelled", but 5.3 pins build_direction_grid to return list[DirectionCell] which has nowhere to put one. no supplied run has a filename that parses to no direction. for now such an image is left out of the grid entirely so nothing renders it - do we return extras from a second function in report/images.py and leave 5.3's signature alone, or widen the return type

10. for now an unusable reading reports only SENSOR_UNAVAILABLE, so a checkpoint whose hub was unreachable and whose sps30 was also dead gives one gap and not two, since the PDF shows one "Sensor unavailable" line and 4.4 wants both directions to match. is that wanted, or should the PDF list the dead sub-devices underneath and the manifest carry a gap for each

11. 3.4 gives one row per zone, but 2.2 does not say a zone holds only one checkpoint. where two checkpoints in a zone disagree about a device flag, for now the whole zone's block is suppressed and the cell names the flag that said so. is that wanted, or should the zone still report the samples taken while the working checkpoint's device was up - which would need an attribution from sample to checkpoint that 2.5 and 2.6 do not provide
