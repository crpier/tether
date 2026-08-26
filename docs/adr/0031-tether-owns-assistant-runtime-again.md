---
status: accepted
---

# Tether owns the assistant runtime again

The stock Open WebUI migration passed its functional and operational gates, but daily use rejected its slower interface, awkward mobile controls, and inability to present Tether's Evidence, Dreaming, and domain workflows without rebuilding a second assistant behind it. Tether therefore restores the complete runtime at `c956fff`: the SolidJS application, host-owned Conversations, Pi execution, Evidence, Dreaming, voice, scheduling, ingestion, and typed verticals.

Open WebUI leaves the active deployment. Its volume and validated post-migration backup remain preserved, but Tether does not import its conversations or native memories. Future UI libraries may replace presentation and client-side streaming state, but the host remains the transcript and domain authority unless another decision changes that ownership.

This supersedes ADR 0030 and restores the decisions that ADR 0030 had superseded.
