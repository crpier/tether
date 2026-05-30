import { createResource, createSignal, For, Show } from "solid-js"
import { render } from "solid-js/web"
import type { MemoryListResponse } from "@tether/shared"
import "./styles.css"

const fetchMemories = async (): Promise<MemoryListResponse> => {
  const response = await fetch("/api/memories")
  if (!response.ok) throw new Error(`Failed to load memories: ${response.status}`)
  return response.json()
}

function App() {
  const [query, setQuery] = createSignal("")
  const [memories] = createResource(fetchMemories)

  return (
    <main class="shell">
      <section class="hero">
        <p class="eyebrow">Tether v0</p>
        <h1>Capture and recall personal context.</h1>
        <p>
          First slice scaffold: Effect HTTP backend, Solid frontend, and shared Memory schemas.
        </p>
      </section>

      <section class="panel">
        <h2>Create Memory</h2>
        <label>
          Title
          <input placeholder="Effect HTTP for Tether" />
        </label>
        <label>
          Body
          <textarea placeholder="Save the context you want future-you or the assistant to recall." rows={6} />
        </label>
        <label>
          Tags
          <input placeholder="tether, architecture" />
        </label>
        <button type="button" disabled>Save Memory — coming next</button>
      </section>

      <section class="panel">
        <h2>Recall Search</h2>
        <input
          value={query()}
          onInput={(event) => setQuery(event.currentTarget.value)}
          placeholder="Search memories"
        />
        <Show when={memories()} fallback={<p>Loading memories…</p>}>
          {(data) => (
            <Show when={data().memories.length > 0} fallback={<p>No memories yet.</p>}>
              <ul>
                <For each={data().memories}>{(memory) => <li>{memory.title}</li>}</For>
              </ul>
            </Show>
          )}
        </Show>
      </section>
    </main>
  )
}

render(() => <App />, document.getElementById("root")!)
