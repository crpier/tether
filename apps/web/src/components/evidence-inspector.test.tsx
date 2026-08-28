import { cleanup, fireEvent, render, screen } from "@solidjs/testing-library";
import { afterEach, describe, expect, test, vi } from "vitest";

import { EvidenceInspector } from "./evidence-inspector";
import { FakeEvidenceHost } from "../testing/fakes/evidence";

const uri = "tether://message/019f0000-0000-7000-8000-000000000001";

afterEach(cleanup);

describe("EvidenceInspector", () => {
  test("shows an unavailable state for missing Evidence", async () => {
    render(() => (
      <EvidenceInspector
        api={new FakeEvidenceHost()}
        onClose={() => undefined}
        uri={uri}
      />
    ));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Evidence is unavailable",
    );
  });

  test("shows the exact promoted email behind a citation", async () => {
    const emailUri = "tether://email/019f0000-0000-7000-8000-000000000003";
    const api = new FakeEvidenceHost([
      {
        body_chars: 50_100,
        body_text: "The apartment is booked for 12-18 June.",
        body_truncated: true,
        captured_at: "2026-08-22T09:30:00Z",
        content_hash: "source-hash",
        date_header: "Tue, 7 Apr 2026 09:30:00 +0000",
        from_header: "Alice <alice@example.com>",
        gmail_message_id: "m1",
        kind: "email",
        subject: "Lisbon booking",
        thread_id: "t1",
        uri: emailUri,
      },
    ]);

    render(() => (
      <EvidenceInspector api={api} onClose={() => undefined} uri={emailUri} />
    ));

    const inspector = await screen.findByRole("dialog", {
      name: "Evidence inspector",
    });
    expect(inspector).toHaveTextContent("Lisbon booking");
    expect(inspector).toHaveTextContent("Alice <alice@example.com>");
    expect(inspector).toHaveTextContent(
      "The apartment is booked for 12-18 June.",
    );
    expect(inspector).toHaveTextContent("Source text truncated");
  });

  test("shows the exact conversation message behind a citation", async () => {
    const onClose = vi.fn();
    const api = new FakeEvidenceHost([
      {
        content: "I prefer aisle seats.",
        conversation_id: "019f0000-0000-7000-8000-000000000002",
        kind: "message",
        message_id: "019f0000-0000-7000-8000-000000000001",
        occurred_at: "2026-08-22T09:30:00Z",
        role: "user",
        seq: 14,
        uri,
      },
    ]);

    render(() => <EvidenceInspector api={api} onClose={onClose} uri={uri} />);

    const inspector = await screen.findByRole("dialog", {
      name: "Evidence inspector",
    });
    expect(inspector).toHaveTextContent("Conversation message");
    expect(inspector).toHaveTextContent("I prefer aisle seats.");
    expect(inspector).toHaveTextContent("Message 14");
    fireEvent.click(screen.getByRole("button", { name: "Close Evidence" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
