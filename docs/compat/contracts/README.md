# Consumer contract fixtures

Each file here is one public Khipu payload as it stands **today** (Phase 1, session B),
built from invented example data, never from real captures. `tests/test_contracts.py`
asserts the code's current output is a *superset* of the matching fixture — every
fixture key present with the same JSON type, `prior_work`'s array/empty-array/null
distinction preserved, and the rendered prompt block's heading/line-shape/footer/600-char
ceiling unchanged. A later phase may add keys freely; removing, renaming, or retyping one
here is the regression this test exists to catch.
