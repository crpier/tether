# The host API contract is generated from Pydantic by a small custom Starlette layer

Tether serves its HTTP API on Starlette, but binds typed handlers, Pydantic request validation and response serialization, and OpenAPI 3.1 generation through a small **custom contract layer** of its own rather than adopting a batteries-included framework. Handlers stay plain typed async callables; their request and response shapes are Pydantic models, and the OpenAPI document is derived from those same models so the schema can never drift from what the code validates at runtime. The API-contract models are kept distinct from the persistence and service models, so the wire format is free to evolve without dragging storage with it.

The reason the schema must come from Pydantic — rather than being hand-written or owned by a framework's own types — is that it is the **single source of truth for two generated clients**: the web frontend's typed client and the pi tool shims (ADR 0005). Both the human-facing surface and the internal tool surface are described by one generated OpenAPI document, so a single contract feeds every consumer.

## Why a custom layer

FastAPI, SpecTree, and apispec were all considered and rejected. FastAPI brings a large framework surface and runtime behavior Tether does not want on top of Starlette's lighter transport. SpecTree hides too much behind dynamic request context. apispec still requires building most of this glue to keep the runtime and the spec from drifting. Since the load-bearing requirement is just "generate the API schema from the Pydantic models actually used at runtime, for the generated clients," a thin Tether-specific layer meets it with less imported behavior than any of the alternatives — and it is deliberately a Tether component, not a reusable framework.

## Why it is hard to reverse

Every REST endpoint, the generated web client, and the generated pi tool shims depend on this contract being the one source the OpenAPI document is built from. Replacing the layer means re-deriving that document a different way and regenerating every downstream client against it. The specific class and helper names that implement the layer are not part of this decision and may be refactored freely; what is costly to reverse is the commitment that the contract is generated from Pydantic and that one OpenAPI document serves both the public and the internal tool surfaces.
