# Settings sync build checkpoint

- Phase: settings-sync build (slices F/G)
- Status: partial; not ready for integration
- Scope: settings replication events and caps; Your Macs switch and conflict resolution; migration design only.
- Oracle budget: up to two focused/full local runs reserved for this phase; root retains the final integration run and visual inspection.
- Mock gate: explicitly waived by the assigned brief; root will inspect screenshots at 1100 and 390 px.

## Evidence

Implemented: typed `setting_set` and cap projection through the existing
routing log/hub path; snapshot-on-enable conflict rows; global owner copy
switch polling while ordinary replication is off; bounded (64-event) upload
fairness before each pull; and the routing-copy periodic-job merge.

Checks: focused settings/cap compatibility and off-Mac on/off propagation
passed (2 tests); upload fairness and off-control recheck passed (2 tests);
priority identity/default-tracking checks passed (2 tests); desktop production
build passed. Full Python suite was started but this runner returned after its
30-second capture window without an exit status. The full Vitest run started
and reported existing layout expectations plus the old routing-copy label
before its capture window ended; the label was restored to include the old
search term and needs a complete rerun.

Unresolved: remote setting application has no owner-actions Undo record yet;
cross-Mac request/answer dedupe/stale semantics are not complete; day-120
retention has only the operations migration note, not a dedicated oracle.
