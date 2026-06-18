# Tether owns chat data; pi runs behind a port

Chat is Tether's front door, and the previous project coupled chat identity and storage to its assistant runtime (OpenCode sessions), which made conversations second-class data we could not search, capture from, or migrate. We decided that Tether persists Conversations, messages, and tool-call records as first-class Tether data, while pi executes the agent loop and tools behind a runtime port; pi session identifiers appear only as Source Refs.

## Considered Options

- **Pi owns sessions, Tether adapts over its API** — less to build, but repeats the OpenCode coupling we already paid for once: chat history becomes hostage to the runtime, and conversation-driven Capture has no stable substrate.
- **Build our own agent loop** — maximum control, but rebuilds session/tool plumbing pi already provides, with no added product value while the runtime stays swappable behind the port.

## Consequences

- Conversations can serve as a Capture source (Loose Memories extracted with the Conversation as Source Ref) without depending on runtime internals.
- The assistant runtime is replaceable without losing chat history.
- Tether must maintain its own conversation persistence and keep it in sync with what the runtime executes — accepted cost of decoupling.
