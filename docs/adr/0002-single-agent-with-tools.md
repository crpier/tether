# One agent with a tool belt, not multiple agents

Tether has a **single agent definition** — one tool belt, one system prompt, one set of (pi) extensions — and no per-domain or specialized sub-agents. Every capability (memory operations, bucket-item operations, media ingestion, cooking, enrichment, …) is a **tool** that single definition exposes.

"One agent" is a definition, not a single OS process. The agent runtime is **pi** in RPC mode, which holds one active session per process and runs turns sequentially. So the Python host spawns **multiple pi processes as it needs them** — e.g. one for foreground chat, ephemeral ones for background work (a Scheduled trigger firing a prompt, Recall-prompt generation) so they don't block the chat. Every such process is the same agent definition; concurrency is achieved by more processes, not by a different agent.

Despite the project being framed around "AI agents," a single-user assistant gains almost nothing from multiple *distinct* coordinating agents and pays for it in coordination, duplicated context, and harder debugging. What makes Tether feel agentic is the tool belt plus persistent memory, not a roster of specialized agents. A specialized sub-agent definition can be reconsidered if a genuinely distinct long-running job ever needs one — but it is not the default architecture.

**Amendment (2026-07-13):** the single persona now ships two system-prompt variants selected by run kind (`apps/host/tether/system_prompt.py`): the full conversation prompt for interactive chat, and a shorter unattended-task prompt for scheduled-trigger and Recall runs. Both variants are the same agent definition — one tool belt, one persona, one set of extensions; only the prompt text differs to fit the run's shape.
