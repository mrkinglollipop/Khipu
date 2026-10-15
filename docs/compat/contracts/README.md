# Consumer contract fixtures

These files pin the shape of Khipu's public payloads. Anything that reads Khipu output (a harness hook, an MCP client, a script) can rely on what is written here. All example data is invented. None of it comes from real captures.

## The fixtures

| File | What it pins |
| --- | --- |
| `khipu_search.result.json` | The result of `khipu_search`, with an episode hit and a topic hit. |
| `khipu_get.episode.json` | The result of `khipu_get` for an episode. |
| `khipu_owed.result.json` | The result of `khipu_owed` (open commitments). |
| `khipu_status.light.json` | The light `khipu_status` payload returned when a prompt is passed and `full` is not set. |
| `khipu_status.full.keys.json` | The full `khipu_status` payload, as a list of key names only. No values or types. |
| `prompt_block.txt` | The rendered "Prior work on this topic" block the prompt hook pushes into context when recall finds matches. This is text, not JSON. |

## The rule

`tests/test_contracts.py` regenerates each payload from the current code, using invented test data (no database, no network). It runs as part of the normal hermetic pytest suite in `packages/cli`.

For the JSON fixtures, the live payload must be a structural superset of the fixture:

- Every fixture key must still be present, with the same JSON type.
- A `null` in a fixture means "this key may be null". It does not require null in every call.
- A list of plain values must still contain every listed value.
- A list of objects is matched by shape, so different row shapes (an episode hit and a topic hit, for example) are checked on their own.
- `prior_work` keeps its distinction between an array, an empty array and null.
- For `khipu_status.full.keys.json`, only the key names are checked.

For `prompt_block.txt`, the rule is stricter. The rendered block must match the fixture text exactly, keep its heading and footer, and stay within the 600-character budget. Changing the wording of the fixed block fails the test.

## Changing a payload

Adding a key to a real payload never fails the test. Removing, renaming or retyping a documented key does, and that is the regression this check exists to catch. If a change is deliberate, update the fixture in the same change and say why.
