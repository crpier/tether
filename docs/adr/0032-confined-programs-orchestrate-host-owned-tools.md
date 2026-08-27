---
status: accepted
---

# Confined programs orchestrate host-owned tools

Tether sometimes needs several dependent or parallel tool calls whose intermediate results are useful to code but not to the model. Giving Pi its native Bash tool would also give generated commands the host process's environment, loopback credentials, durable `/data`, and Pi session files. A working directory or command allowlist would not isolate those authorities.

Tether therefore exposes one additive `execute_tools` capability backed by a fresh confined TypeScript/JavaScript interpreter. A program receives only the generated Tether tool catalog. It has no ambient filesystem, process, environment, network, import, package, or persistence access. The interpreter bounds elapsed time, admitted tool calls, and retained output. The outer Pi tool call is the transcript identity; nested calls are transient orchestration details.

Generated programs do not bypass Tether. Every nested call still enters its existing host endpoint, which owns authentication, parameter validation, authorization, durable effects, quotas, and tracing. Direct tools remain available for simple one-call work. Pi remains the sole agent loop, and Tether remains the transcript and domain authority.

Any future Bash capability must run in a separate ephemeral sandbox with no host data or credential mounts, no network, a read-only root filesystem, dropped capabilities, strict resource limits, and scratch-only storage. Bash must not be enabled in the co-resident production Pi process.
