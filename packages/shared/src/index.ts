import { Schema } from "effect"

export const SourceRefType = Schema.Literal(
  "manual",
  "url",
  "conversation",
  "file",
  "pi_session"
)
export type SourceRefType = Schema.Schema.Type<typeof SourceRefType>

export const SourceRef = Schema.Struct({
  type: SourceRefType,
  label: Schema.optional(Schema.String),
  uri: Schema.optional(Schema.String),
  externalId: Schema.optional(Schema.String),
  capturedAt: Schema.String
})
export type SourceRef = Schema.Schema.Type<typeof SourceRef>

export const Memory = Schema.Struct({
  id: Schema.String,
  title: Schema.String,
  body: Schema.String,
  tags: Schema.Array(Schema.String),
  sourceRefs: Schema.Array(SourceRef),
  createdAt: Schema.String,
  updatedAt: Schema.String,
  deletedAt: Schema.NullOr(Schema.String),
  markdownPath: Schema.String
})
export type Memory = Schema.Schema.Type<typeof Memory>

export const CreateMemoryInput = Schema.Struct({
  title: Schema.String,
  body: Schema.String,
  tags: Schema.Array(Schema.String),
  sourceRefs: Schema.optional(Schema.Array(SourceRef))
})
export type CreateMemoryInput = Schema.Schema.Type<typeof CreateMemoryInput>

export const UpdateMemoryInput = Schema.Struct({
  title: Schema.optional(Schema.String),
  body: Schema.optional(Schema.String),
  tags: Schema.optional(Schema.Array(Schema.String)),
  sourceRefs: Schema.optional(Schema.Array(SourceRef))
})
export type UpdateMemoryInput = Schema.Schema.Type<typeof UpdateMemoryInput>

export const MemoryListResponse = Schema.Struct({
  memories: Schema.Array(Memory)
})
export type MemoryListResponse = Schema.Schema.Type<typeof MemoryListResponse>
