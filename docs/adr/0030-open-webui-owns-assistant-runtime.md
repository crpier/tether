---
status: accepted
---

# Open WebUI owns the assistant runtime

Stock Open WebUI owns the generic assistant interface and all state needed to run it: accounts, browser sessions, conversations, provider configuration, model execution, native tool calling, approvals, files, voice, generic memory, and optional web search. Its transcript is the sole conversational authority. Tether does not import, mirror, project, or synchronize Open WebUI messages.

Tether is a headless domain capability host. It retains Health Connect, Bucket items, and Todos because their typed state is useful independently of the former assistant implementation. Open WebUI reaches exactly 17 allowlisted bearer-authenticated operations across those domains. Bucket search is deterministic SQLite rather than model-backed retrieval. Capture clients keep their existing origin and independent bearer credential.

The Pi runtime, TypeScript agent, Tether chat application, browser conversation APIs, model-backed background work, writable Tether Memory, scheduling, proposals, optional content integrations, and custom assistant presentation are deleted in one release. Old tables are left inert so operational rollback can start the previous image, but there are no compatibility adapters or dual-running runtimes. Open WebUI runs from an exact official image digest with its own durable volume and no mount of Tether data, host files, credentials, or the Docker socket.

Using a Pipe to preserve the old agent loop, synchronizing transcripts, importing old conversations, and recreating feature parity were rejected because each would retain the maintenance burden this migration removes. Operational rollback restores the old Git revision and image; it is not an application mode in the new architecture.

Open WebUI `v0.11.1` tool approvals are experimental and do not protect Automations. Interactive tools start in approval mode, arbitrary code execution remains disabled, and Automations remain disabled for the first release.
