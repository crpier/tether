---
status: accepted
---

# Each Integration cluster presents exactly one interface

The "Separate X concerns" refactor wave split every external-service integration into many cohesive files (YouTube: 19, Gmail: 11, Health Connect: 12) but gave none of them an external interface — callers import internals directly (`youtube_store` from 10 places, `health_connect_contracts` from 11), so each cluster's interface is its file list. We decided each Integration (CONTEXT.md) becomes a package whose `__init__.py` is interface-only — re-exports enumerated from real external call sites, no implementation — with all other modules as internal seams that only the cluster's own code and tests may import. A gate test enforces this mechanically: nothing outside `tether/<cluster>/` imports `tether.<cluster>.` submodules. YouTube, Gmail, and Health Connect convert now; smaller groupings (Readwise, Kosync, ebook stats) stay flat until their internals leak across call sites — same trigger logic as ADR-0016's rule of three.

This complements ADR-0016 rather than amending it: verticals remain fully bespoke with no shared code between Integrations; only the code shape inside each bespoke slice changes. The one deliberate exception is infrastructure plumbing with no domain shape of its own — Google OAuth mechanics live in the shared `tether.google_oauth` module because both YouTube and Gmail build on them. Shrinking an interface below the current call-site union is welcome later work, done by removing callers, not by hiding imports.
