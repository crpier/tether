# Captured memories are provisional until human review

Captured memories start **loose** — stored but untrusted, and excluded from the corpus the agent retrieves over. A memory only becomes **tethered** (trusted and retrievable) after the human reviews it. The assistant reasons exclusively from tethered memories.

Review is AI-assisted but never AI-decided: the agent does the toil (deduplication, conflict detection against the existing corpus, summarizing, pattern-spotting) and proposes, but a human gives final approval on every tether. This keeps judgment on the human for the cases that need it without making review pure manual toil — the failure mode that would otherwise leave the corpus empty and ADR 0001's bet unfulfilled.

We accept the cost — capture friction and a review backlog the human must work through — to guarantee a high-trust corpus that the agent cannot silently poison with unreviewed, low-confidence, or wrong captures. This is deliberate and differs from the common approach of trusting captured memories immediately; retrieval queries filter to tethered, so reversing it would touch the Memory data model and every read path.
