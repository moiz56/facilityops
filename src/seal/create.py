"""Seal a run: write seal_<run_id>.json next to the report and manifest.

Hashes the record, every evidence file and the outputs, and links to the
previous run's seal. A missing previous seal is a chain break, reported,
never re-chained.
"""
