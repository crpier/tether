---
status: accepted
---

# Typed capabilities own action policy

Tether has no generic Proposal queue, action registry, or earned-autonomy ledger. The abstraction added a second lifecycle, execution path, tool pair, configuration set, and large review interface, but its only concrete producer was an optional Gmail backlog sweep. Direct user-requested Gmail tools already provide a clearer path.

Each typed capability now owns whether an action is allowed and how it executes. Human-requested chat actions call those capabilities directly. A future unattended feature that needs review must justify and own a domain-specific interface rather than revive a generic approval system.

The Proposal and Gmail-purge runtime, tools, routes, configuration, and SPA are removed. Existing production Proposal tables and rows remain inert so an operational rollback can run an older image; fresh databases do not create them. Recall's advisory essay-grade result is a separate learning interaction and remains unchanged.

This supersedes ADR 0014.
