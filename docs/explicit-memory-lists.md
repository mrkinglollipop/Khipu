# Explicit asks and searchable memory

Capturing a conversation stores its summary, original words, episode decision
strings, preferences and topics. It does not create, close, reassign or tidy
commitments, create decision registry rows, or detect decision reversals.

`khipu_owed_update(action="ask", requester=..., question=..., yes=..., no=...)`
files an ask deliberately. `khipu_needs_you` includes those questions.
`khipu_owed` and explicit updates remain available, including manual changes
to archived rows. The ledger remains the work record.

Session start includes recent episodes and linked topics. Search uses the
owner's original words and summaries, including episode decision strings.
Search legs, embeddings and the topic graph retain their behavior.

Existing commitment, decision and link rows, provenance and change records,
exports, replicas and embedding data remain. Archived decision validity is
still read by search and episode detail. Nothing in this removal migrates,
deletes or re-embeds stored memory. Old extraction gap rows remain evidence;
nightly replay selects only the still-supported deliverable steps.
