# FastAPI owns the host REST contract

Tether uses FastAPI's native routers, Pydantic request validation, response models, and OpenAPI generation instead of maintaining a custom contract layer over Starlette. The custom layer duplicated framework behavior, imposed nonstandard validation details, and became harder to understand than FastAPI's established conventions; FastAPI keeps runtime validation and generated clients on the same Pydantic source of truth while plain internal ASGI routes remain excluded from the public schema.
