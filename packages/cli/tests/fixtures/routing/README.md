# Job runner fixtures (routing truth, phase 1b)

Shaped like a real lanes runner's files (`config.json`, `events.log`,
`state/global.json`, `state/heartbeat.json`) with every name changed: two
projects (A and B), generic lane names, made-up pipelines, jobs and paths.
The times sit around 2026-10-14T15:00Z, the clock `tests/test_routing_sources.py`
starts from. Keys of `config.json` that Khipu must never keep carry the
word CANARY so a test can prove they never reach the record.
