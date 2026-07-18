# Presentation splits into constrained inline Widgets and sandboxed freeform Artifacts

Tether's agent needs to show more than chat text — tables, charts, quiz UIs, generated pages — but a single generation path for all of it forces a bad trade-off: constrain everything (safe but limited) or sandbox everything (flexible but heavyweight for a simple table). We split presentation into two kinds instead of picking one:

- **Widgets** — inline, vetted, Tether-styled render specs (tables, Mermaid, Vega-Lite) rendered directly in the chat turn. Safe *because* the vocabulary is constrained: the agent picks from a known set of spec shapes, so there is no arbitrary code to sandbox.
- **Artifacts** — freeform, agent-generated pages, rendered in an iframe under a strict CSP, versioned, and linked from chat. Free to be anything (a game, a custom form, a Lesson) *because* they're sandboxed rather than vetted.

Widgets can't express arbitrary interactivity; Artifacts can't be trusted to read back into the conversation directly. We resolve the second half with **Artifact events**: an append-only JSON record an Artifact posts about itself (a quiz answer, a form submission). This is the *sole* talk-back channel — the agent never reads an Artifact's rendered content or DOM, only the events it explicitly posts. That keeps the trust boundary at the sandbox edge: an Artifact can be given real freedom to render because nothing it does can affect the agent's state except through a narrow, structured, append-only channel it opts into.

## Considered options

- **One generation path, always sandboxed** — simplest mental model, but pays iframe/CSP overhead and loses the "vetted, Tether-styled" consistency for the common case (a table, a chart) where it isn't needed.
- **One generation path, always vetted/constrained** — no sandbox needed, but caps what's expressible; quizzes, games, and truly freeform Lessons don't fit a fixed vocabulary.
- **Let Artifacts talk back via arbitrary postMessage / DOM read** — rejected: turns every Artifact into an unbounded input surface the agent must treat as untrusted in unpredictable ways, instead of a narrow structured event log.

## Consequences

- Two rendering/generation code paths to maintain instead of one, and a judgment call per feature about which kind fits — expected to be obvious in practice (a chart is a Widget; a Lesson is an Artifact).
- Artifacts are hard to reverse once shipped as a channel: any future capability that wants richer agent↔Artifact interaction has to extend the Artifact-event shape, not bypass it.
- Widgets stay a small, curated vocabulary by design; adding a new Widget type is a deliberate, reviewed addition, not something the agent can invent freely the way it invents Facets.
