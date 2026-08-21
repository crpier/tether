import { cleanup, fireEvent, screen, within } from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import { FakeHost, dreamRun, renderApp } from "../testing/harness";

afterEach(cleanup);

describe("Dreaming panel", () => {
  test("surfaces a history loading failure instead of claiming there are no runs", async () => {
    const host = new FakeHost({ authenticated: true });
    host.dreaming.listDreamRuns = () =>
      Promise.reject(new Error("history unavailable"));
    renderApp(host, undefined, { path: "/browse/dreaming" });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load Dream history",
    );
    expect(screen.queryByText("No Dream runs yet")).not.toBeInTheDocument();
  });

  test("surfaces a selected run detail failure", async () => {
    const run = dreamRun({
      conversation_title: "Missing detail",
      id: "019f0000-0000-7000-8000-000000000031",
      status: "success",
    });
    const host = new FakeHost({ authenticated: true, dreamRuns: [run] });
    renderApp(host, undefined, { path: "/browse/dreaming" });

    fireEvent.click(
      await screen.findByRole("button", { name: /Changed.*Missing detail/ }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load this Dream run's details",
    );
  });

  test("shows fact-level additions and removals for a changed run", async () => {
    const run = dreamRun({
      conversation_title: "Travel preferences",
      id: "019f0000-0000-7000-8000-000000000041",
      mutation_count: 1,
      status: "success",
    });
    const host = new FakeHost({
      authenticated: true,
      dreamRunDetails: {
        [run.id]: {
          mutations: [
            {
              actor: "dream",
              attempts: 2,
              created_at: "2026-08-21T08:01:03Z",
              error: null,
              fact_changes: [
                {
                  kind: "removed",
                  text: "Prefers window seats.",
                  topic: "Travel",
                },
                {
                  kind: "added",
                  text: "Prefers aisle seats.",
                  topic: "Travel",
                },
              ],
              id: "019f0000-0000-7000-8000-000000000042",
              operation: "write",
              status: "acknowledged",
              tool_call_id: "tool-travel",
              updated_at: "2026-08-21T08:01:04Z",
              workspace_path: "conversation-id/run-id.md",
            },
          ],
          run,
        },
      },
      dreamRuns: [run],
    });
    renderApp(host, undefined, { path: "/browse/dreaming" });

    fireEvent.click(
      await screen.findByRole("button", {
        name: /Changed.*Travel preferences/,
      }),
    );

    const changes = await screen.findByRole("list", { name: "Fact changes" });
    expect(within(changes).getByText("−")).toBeInTheDocument();
    expect(
      within(changes).getByText("Prefers window seats."),
    ).toBeInTheDocument();
    expect(within(changes).getByText("+")).toBeInTheDocument();
    expect(
      within(changes).getByText("Prefers aisle seats."),
    ).toBeInTheDocument();
    expect(screen.getByText("conversation-id/run-id.md")).toBeInTheDocument();
  });

  test("summarizes active work, the last change, and failures", async () => {
    const host = new FakeHost({
      authenticated: true,
      dreamRuns: [
        dreamRun({
          completed_at: null,
          conversation_title: "Active conversation",
          id: "019f0000-0000-7000-8000-000000000021",
          status: "running",
        }),
        dreamRun({
          conversation_title: "Changed conversation",
          id: "019f0000-0000-7000-8000-000000000022",
          mutation_count: 1,
          status: "success",
        }),
        dreamRun({
          conversation_title: "Failed conversation",
          id: "019f0000-0000-7000-8000-000000000023",
          status: "failed",
        }),
      ],
    });
    renderApp(host, undefined, { path: "/browse/dreaming" });

    const summary = await screen.findByRole("region", {
      name: "Dreaming status",
    });

    expect(summary).toHaveTextContent("1 active run");
    expect(summary).toHaveTextContent("Last change");
    expect(summary).toHaveTextContent("1 failed run");
  });

  test("filters run history by meaningful outcome", async () => {
    const host = new FakeHost({
      authenticated: true,
      dreamRuns: [
        dreamRun({
          conversation_title: "Changed conversation",
          id: "019f0000-0000-7000-8000-000000000011",
          mutation_count: 2,
          status: "success",
        }),
        dreamRun({
          conversation_title: "No-op conversation",
          id: "019f0000-0000-7000-8000-000000000012",
          status: "no_op",
        }),
        dreamRun({
          conversation_title: "Failed conversation",
          error: "model timed out",
          id: "019f0000-0000-7000-8000-000000000013",
          status: "failed",
        }),
      ],
    });
    renderApp(host, undefined, { path: "/browse/dreaming" });

    const panel = await screen.findByRole("region", { name: "Dreaming" });
    const filters = await within(panel).findByRole("tablist", {
      name: "Dream run filter",
    });
    fireEvent.click(within(filters).getByRole("tab", { name: "Failed" }));

    expect(
      await within(panel).findByRole("button", {
        name: /Failed.*Failed conversation/,
      }),
    ).toBeInTheDocument();
    expect(
      within(panel).queryByRole("button", {
        name: /Changed.*Changed conversation/,
      }),
    ).not.toBeInTheDocument();
    expect(
      within(panel).queryByRole("button", {
        name: /No changes.*No-op conversation/,
      }),
    ).not.toBeInTheDocument();
  });
});
