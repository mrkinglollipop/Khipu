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
| `decision_details` | Eight real session transcripts, three runs each on two of them | Attribution (`by`) present on every detail; `rationale` and `reverses` almost always empty. The count of plain decisions varies run to run with and without the block, and the sampled sessions were still changing, so the comparison is not clean. | **Not enabled.** Needs frozen transcripts. |
| `auto_supersede` | Depends on `reverses` from `decision_details` | No reversal was produced in the sample | **Not enabled.** |
| `briefs` | Unit and scratch-database tests only | Not yet run against the production hub or a real model | **Not enabled.** |

**The relevance floor's number.** On the production embedding profile the best match for the 20 unseen subjects scored a cosine of 0.57 to 0.64, and the best match for the 149 real prompts scored 0.67 to 0.82. The floor is 0.65. A first version that filtered row by row did nothing, because one common word ("rules", "train", "history") was enough to keep a row; the gate that replaced it asks whether any row in the list is evidence and otherwise leaves the list alone.

**Defects the gates found.** Two were in code that had only ever run against stubs.

- With `decision_details` on, the model often wrote a decision only in the detail block, and a detail that matched no listed decision was dropped. A session that yielded seven decisions without the block yielded none with it. A detail's text now joins the decision list.
- A malformed detail block made the whole model answer unparseable, and the capture job retried without end. The block is now cut out and the rest parsed.

**What is not covered.** The unseen subjects are far from anything in memory; a prompt on a nearby subject that memory does not hold will still get its nearest neighbours. The blind judge is a model, not the user. Latency was measured on a machine under heavy unrelated load.
