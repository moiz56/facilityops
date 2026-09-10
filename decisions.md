# Decisions

1. Hatchling used to build backend 

2. branding.yaml is a standalone file but the config/report.yaml in the document contains a branding section, for now I have put both in the config.yaml making branding.yaml empty (can be discussed)

3. common/schema.py implemented, some helper classes were made like Pose, Point2D to make it easier to structure data

4. common/loader.py implemented. Nothing is coerced: a wrong type is refused, left unset, and recorded as a FieldAnomaly, so a confidence written as "0.94" never reaches the page as a measurement (TA-27). Only an unusable record raises - unreadable file, invalid JSON, non-object root, or no run_id, since 4.1 names the output files from it. Everything else loads with the problem recorded.

5. FieldAnomaly.item_id is a checkpoint_id where the field is a checkpoint's, __run__ for a run-level field, or a locator like sensor_alerts[3] for an array entry with no id. Alerts and samples never borrow their nearest_checkpoint_id: that is proximity, not attribution (2.5).

6. confidence 0.0 resolves to None in the loader, per 2.9. The difference between an absent key and a recorded 0.0 is not kept, because 2.9 gives them the same meaning.

7. Keys section 2 does not describe are logged at debug and counted in the run summary, not warned about. An extra key loses nothing; a refused value does.

8. report/cli.py carries only the stages of 5.2 that are built - currently load and validate. Stages join as their modules are written, and --list-stages and --stop-after read from that list, so both stay accurate.

9. `--dump-record` writes the parsed record as JSON, to a file or to stdout. Not a deliverable - the outputs are the PDF and the manifest (4.1) - but it makes the loader's reading of a file inspectable, and gives an evidence artifact for the parsing tests rather than a claim (deliverable 11). Datetimes and paths are written as text, so the dump reads as what the loader made of the record, not as the record.

# Questions to raise

1. branding.yaml is a standalone file but the config.yaml in the document contains a branding section, for now I have put both in the config.yaml making branding.yaml empty but is this the way to go
2. raise question as to how it is expected the data is fed
3. questions regarding objects , the one shown over there. for now I have given each a deterministic shape but if they are dynamic schema.py must be adjusted

4. a field of the wrong type has no gap type in 4.3, which is a closed set of ten. Confirm these belong in the PDF but not in the manifest gaps array (TA-27 says "handled or reported clearly", not which).

5. 2.5, 2.6 and 2.7 give no optional/required column, only JSON examples. The fields treated as required there are a judgement and would change if a supplied record disagrees.

