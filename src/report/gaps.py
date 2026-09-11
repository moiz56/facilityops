"""Gap detection and classification.

PROVISIONAL, pending question 7 in decisions.md - to be confirmed with the
stakeholder before delivery. MISSING_THERMAL fires for any direction carrying a
*referenced* RGB with no thermal counterpart, whether or not that RGB resolves.
A zero-byte RGB therefore reports both MISSING_IMAGE and MISSING_THERMAL for
the same direction.

The alternative reading of 4.3 - "a direction has an RGB image" meaning a
usable one - is rejected for now because a single broken file would then
suppress a second, unrelated gap, and a thermal that was never captured would
go unreported. Whichever reading is confirmed, it changes the gap count in the
manifest, and 4.4 tests the manifest against the PDF in both directions.
"""
