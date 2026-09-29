**Khipu memory evidence — switch gates, 29 September 2026**

Read [the scope](../plans/2026-09-27-memory-reasoning-scope.md) first. It requires each quality component to stay off until it shows repeatable benefit on its target cases with no regression. This is the record of those gates: what was measured, on what, and what was decided. A rejected component stays in the code behind its switch.

**How each gate was run**

- *Local prompt lane.* A frozen copy of the production replica (10,206 episodes, 1,293 topics, 40,641 decisions, schema version 3) and 149 real prompts sampled from the query log with their recorded query vectors, so two runs differ only in the code or switch under test.
- *Explicit search.* The production hub, read only, with the configured embedding and synth models (`gemini-embedding-2`, `gemini-2.5-flash`).
- *Blind judgment.* Where a switch changed the top three results, both lists went to a judge that did not know which was which, with the sides shuffled per item.
- *Unseen subjects.* Twenty prompts on subjects the memory has never held (cooking, travel, gardening and the like), to test abstention.

**Results**

| Switch | Measured | Result | Decision |
|---|---|---|---|
| `validity_ranking` | 149 real prompts, on against off | 149 identical. Production holds no superseded decision yet, so there is nothing to rank down. Scenario tests cover the behaviour. | On |
| `time_interpretation` | 149 real prompts, on against off | 149 identical. None of the sampled prompts carries a time phrase. Scenario tests cover the behaviour. | On |
| `graph_candidates` | 149 real prompts; the top three changed on 81; blind judgment of those 81 | Off preferred 59, on preferred 4, tie 18 | **Rejected.** Stays off. |
| `rerank` | Golden set, 8 entries | Expected result moved up on 3, down on 0 | |
| `rerank` | 40 real queries through explicit search; the top three changed on 34; blind judgment of those 34 | On preferred 28, off preferred 1, tie 5. Adds a median 0.8 s to a search that takes about 5 s. One model request per search. | On |
| `relevance_floor` | 149 real prompts, on against off | 149 identical: the gate never empties or alters a real prompt's results | |
| `relevance_floor` | 20 unseen subjects | 17 abstain with the gate on, 0 with it off | |
| `relevance_floor` | Golden set, explicit path | 8 of 8 positives found, none emptied | On |
| `reflect` | Three questions against the production hub | Two answered with every claim cited to an offered source; the unseen subject abstained. 8 to 13 s per question. | On. It runs only when asked. |
| `decision_details` | Ten finished session transcripts, frozen, two runs per arm on identical input | Decisions per run: 4.6 without the block, 4.3 with it; runs yielding none: 4 of 20 without, 5 of 20 with. With it, every detail is attributed, about 60% carry a rationale and a few name what they reverse. One transcript of ten yielded decisions on both runs without the block and none on both runs with it. | **Not enabled.** The gain is real, and so is the risk of losing a session's decisions. |
| `auto_supersede` | Depends on `reverses` from `decision_details` | Follows `decision_details` | **Not enabled.** |
| `briefs` | Two topics built against the production hub with the configured model, then rebuilt | Both built with every kept claim cited; one uncited claim was removed; the rebuild did no work because the sources had not changed. 525 topics are planned, 3.5 million characters in all. | On. The nightly builds at most 40 a night. |

**The relevance floor's number.** On the production embedding profile the best match for the 20 unseen subjects scored a cosine of 0.57 to 0.64, and the best match for the 149 real prompts scored 0.67 to 0.82. The floor is 0.65. A first version that filtered row by row did nothing, because one common word ("rules", "train", "history") was enough to keep a row; the gate that replaced it asks whether any row in the list is evidence and otherwise leaves the list alone.

**Defects the gates found.** Two were in code that had only ever run against stubs.

- With `decision_details` on, the model often wrote a decision only in the detail block, and a detail that matched no listed decision was dropped. A session that yielded seven decisions without the block yielded none with it. A detail's text now joins the decision list.
- A malformed detail block made the whole model answer unparseable, and the capture job retried without end. The block is now cut out and the rest parsed.

**Extraction is noisy with or without the block.** On identical input two runs of today's extraction returned 10 and 0 decisions for one transcript and 5 and 15 for another. That is a property of the existing extraction, not of this work, and it bounds how small a difference any of these comparisons can detect.

**A rejected speed-up: an indexed keyword search.** A trigram index in the replica answered the same `LIKE` patterns as the scan and returned identical results on all 149 real prompts. It was 1.5 times faster at the median (about 160 ms against 240 to 340 ms) and no faster at the 95th percentile (about 560 to 610 ms against 580 to 780 ms), because a common word still matches thousands of rows. It would have added 73 MB to a 266 MB replica and a set of triggers to keep right. Timeouts are a tail problem, so it was not merged.

**Where a prompt's time goes.** Measured on the production replica: the embedding request itself about 300 ms, a fresh TLS connection about 100 ms, reading the key about 60 ms, the project lookup 50 to 150 ms, loading 20,000 vectors from disk for the scan 70 to 250 ms, and the keyword scan 160 to 340 ms beside them. One prompt lands between 750 and 1,000 ms against an internal deadline of 950 ms, before any load on the machine.

**The warm recall service.** A long-lived local process answers the per-prompt hook over a Unix socket, so a prompt no longer pays for a new interpreter, the package imports, a new TLS connection, the key lookup or reloading the vectors. The hook falls back to the one-shot path when the service is not running. Measured through the launcher on 25 distinct prompts per arm, alternating:

| Machine | Path | Hook wall time, median | 90th percentile | Worst |
|---|---|---|---|---|
| Load 11 | service | 418 ms | 501 ms | 662 ms |
| Load 11 | one-shot | 701 ms | 874 ms | 1,031 ms |
| Load 40 rising to 92 | service | 677 ms | 795 ms | 900 ms |
| Load 40 rising to 92 | one-shot | 926 ms | 1,032 ms | 1,116 ms |

Both paths named the same memories. What remains is the embedding request itself, about 300 ms.

**What is not covered.** The unseen subjects are far from anything in memory; a prompt on a nearby subject that memory does not hold will still get its nearest neighbours. The blind judge is a model, not the user. Latency was measured on a machine under heavy unrelated load.
