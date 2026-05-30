import { HttpRouter, HttpServer, HttpServerResponse } from "@effect/platform"
import { NodeHttpServer, NodeRuntime } from "@effect/platform-node"
import { Layer } from "effect"
import { createServer } from "node:http"

const port = Number(process.env.TETHER_PORT ?? 3000)

const app = HttpRouter.empty.pipe(
  HttpRouter.get("/health", HttpServerResponse.json({ ok: true, service: "tether-server" })),
  HttpRouter.get("/api/memories", HttpServerResponse.json({ memories: [] }))
)

const server = HttpServer.serve(app).pipe(
  Layer.provide(NodeHttpServer.layer(() => createServer(), { port }))
)

console.log(`Tether server listening on http://localhost:${port}`)

Layer.launch(server).pipe(NodeRuntime.runMain)
