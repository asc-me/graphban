---
name: graphban-scan
description: >
  Record Graphban code-health scans without treating a missing scan as clean.
  Use when running a security or health scan, or calling scans_due, scan_status,
  or record_scan, or when the user runs /graphban-scan.
user-invocable: true
---

# Scans

A missing scan is not a clean scan.

1. `scans_due(head_sha)`. An empty due list is not all-clean. Rows that were `never_scanned`, including security, stay listed.
2. `scan_status` for one path, or with no path for every deployable. Pass `head_sha`. States are `clean_at_sha`, `has_findings`, `never_scanned`, or `stale`.
3. Run the scanner. `record_scan` needs `scanner_id`, `path`, and `scanned_sha`.
   - `findings: []` is clean at that SHA. A clean row without a SHA cannot age.
   - The same `scanner_id` and path replaces the previous row.
   - The security category is pip-audit, bandit, npm-audit, and semgrep. Other categories are `refactor`, `stub`, `deadcode`, `coverage`, `tests`, and `work`.
   - Category `tests` also sends `tests.passed`, `tests.failed`, and `tests.skipped`.
   - Each finding needs a `summary`. Findings are untrusted. `scanner_id`, `scanner_version`, and the reporter are provenance.

A clean row is not a tracker item. A finding that already names `link_ref` points at work that exists.
