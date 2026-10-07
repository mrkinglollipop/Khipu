# Embeddings screen mock (approved by Matt 2026-10-07)

Static mock of the Settings "Embeddings" screen and its dialogs for the library-sources and
bring-your-own-embeddings work (`docs/plans/2026-10-07-library-sources-byoe.md`). Open any `.html` in a
browser; `gen.py` regenerates them. `App.css` is a copy of the app stylesheet at the time; the real
screen uses the app's own. `mock.css` holds mock-only layout. The approved states:

- `embeddings-list` (light/dark): models then libraries on one screen
- `embeddings-add-model`, `embeddings-estimate`, `embeddings-coverage`, `embeddings-ready`,
  `embeddings-delete-confirm`, `embeddings-import-receipt`, `embeddings-job-failed`, `libraries`
