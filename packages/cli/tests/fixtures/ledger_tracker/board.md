# Orchestrator Tracker (generated 2026-10-15T16:02:45Z)

Total rows: 14

## Counts by state
- VERIFIED: 1
- MERGED-UNVERIFIED: 1
- BUILT-UNMERGED: 1
- IN FLIGHT: 3
- QUEUED: 2
- NOT STARTED: 1
- NEEDS MATT: 2
- DROPPED: 1
- ON HOLD: 1
- DONE: 1

## Counts by kind
- audit-item: 5
- drive: 2
- wave: 4
- train: 1
- loose-end: 1
- needs-matt: 1

## Rows (grouped by kind, then parent; id order within group — never reordered by state)

### audit-item

**(no parent / root)**
- 412 · BUILT-UNMERGED · Bare number item · PR 412 · owner=lane-c · updated=2099-01-02T00:00:00Z
**parent: 412 — Bare number item**
- a-10 · MERGED-UNVERIFIED · Audit item ten · merged · owner=lane-c · updated=2020-06-01T00:00:00Z
- a-2 · DONE · Audit item two ·  · owner=lane-c · updated=2020-06-02T00:00:00Z
**(no parent / root)**
- b-root · ON HOLD · Another root · parked · owner=lane-d · updated=2020-06-04T00:00:00Z
**parent: gone-parent — (unknown parent)**
- a-orphan · DROPPED · Parent is absent · superseded · owner=- · updated=2020-06-03T00:00:00Z
### drive

**(no parent / root)**
- d-alpha · IN FLIGHT · Drive alpha · plan approved · owner=lane-a · updated=2099-01-01T00:00:00Z
- d-beta · QUEUED · Drive beta ·  · owner=lane-a · updated=2020-07-03T00:00:00Z
### wave

**parent: d-alpha — Drive alpha**
- w-1 · QUEUED · Wave one ·  · owner=none · updated=2020-05-01T10:00:00Z
- w-2 · NOT STARTED · Wave two ·  · owner=unassigned · updated=2020-05-02T10:00:00Z
**parent: d-beta — Drive beta**
- w-3 · IN FLIGHT · Wave three under beta ·  · owner=lane-a · updated=2020-07-04T00:00:00Z
**(no parent / root)**
- w-root · VERIFIED · Wave without a drive · PR 9 merged · owner=lane-b · updated=2020-05-03T10:00:00Z
### train

**(no parent / root)**
- t-1 · NEEDS MATT · Train one · pick a date · owner=none · updated=2099-01-03T00:00:00Z
### loose-end

**parent: t-1 — Train one**
- le-1 · IN FLIGHT · Loose end one ·  · owner=lane-e · updated=2099-01-04T00:00:00Z
### needs-matt

**(no parent / root)**
- nm-1 · NEEDS MATT · Needs a call · two options · owner=none · updated=2020-07-02T00:00:00Z

## Changed since 2026-10-14T16:02:45Z

- 412 · BUILT-UNMERGED · Bare number item · updated=2099-01-02T00:00:00Z
- d-alpha · IN FLIGHT · Drive alpha · updated=2099-01-01T00:00:00Z
- le-1 · IN FLIGHT · Loose end one · updated=2099-01-04T00:00:00Z
- t-1 · NEEDS MATT · Train one · updated=2099-01-03T00:00:00Z
