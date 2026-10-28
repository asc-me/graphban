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
2. `scan_status` for one path, or with no path for every deployable. Pass `head_sha`. States are `clean_at_sha`, `has_findings`, `never_scanned`, `stale`, `tool_missing`, `not_applicable`, `incomplete`, or `not_computable`.
3. Run the scanner. `record_scan` needs `scanner_id`, `path`, and `scanned_sha`.
   - Send `result`, one of four:
     - `complete`: the scanner finished. `findings: []` is then clean at that SHA.
     - `tool_missing`: the scanner is not installed. Not clean; it stays due.
     - `not_applicable`: the scanner does not apply here. It stops being due.
     - `incomplete`: output without a finish. Send the findings it produced; it stays due.
   - `findings: []` without `result: complete` is refused and nothing is stored. An empty list alone is not a clean scan.
   - Every result needs `scanned_sha`, so it goes stale when HEAD moves.
   - The same `scanner_id` and path keeps the previous rows. The newest is the cell.
   - The security category is pip-audit, bandit, npm-audit, and semgrep. Other categories are `refactor`, `stub`, `deadcode`, `coverage`, `tests`, and `work`.
   - Category `tests` also sends `tests.passed`, `tests.failed`, and `tests.skipped`.
   - Each finding needs a `summary`. Findings are untrusted. `scanner_id`, `scanner_version`, and the reporter are provenance.

A clean row is not a tracker item. A finding that already names `link_ref` points at work that exists.
