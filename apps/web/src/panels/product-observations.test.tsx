import {
  cleanup,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@solidjs/testing-library";
import { afterEach, describe, expect, test } from "vitest";

import {
  FakeHost,
  navigateTo,
  productObservation,
  renderApp,
} from "../testing/harness";

afterEach(cleanup);

describe("Feedback panel", () => {
  test("shows the user's wording and the interpreted expected behavior", async () => {
    const host = new FakeHost({
      authenticated: true,
      productObservations: [
        productObservation({
          interpretation: "Resurface same-day exercise intentions.",
          wording: "You should have reminded me about that workout.",
        }),
      ],
    });
    renderApp(host);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("tab", { name: "Feedback" }));

    const row = await screen.findByRole("listitem", {
      name: "Product observation: Resurface same-day exercise intentions.",
    });
    expect(row).toHaveTextContent("Resurface same-day exercise intentions.");
    expect(row).toHaveTextContent(
      "You should have reminded me about that workout.",
    );
  });

  test("an invalidate frame refetches observations recorded in Chat", async () => {
    const host = new FakeHost({ authenticated: true });
    const bus = renderApp(host);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("tab", { name: "Feedback" }));
    await screen.findByText("No open product observations");
    await waitFor(() => {
      expect(host.productObservations.listCalls).toBeGreaterThan(0);
    });
    const before = host.productObservations.listCalls;
    host.productObservations.storedObservations = [
      productObservation({ interpretation: "Newly recorded feedback." }),
    ];

    bus.emit({ keys: ["product-observations"], type: "invalidate" });

    await waitFor(() => {
      expect(host.productObservations.listCalls).toBeGreaterThan(before);
    });
    expect(
      await screen.findByRole("listitem", {
        name: "Product observation: Newly recorded feedback.",
      }),
    ).toBeInTheDocument();
  });

  test("resolving an observation removes it from the open list", async () => {
    const observation = productObservation({
      id: "018f0000-0000-7000-8000-0000000000f1",
      interpretation: "Resurface same-day exercise intentions.",
      version: 3,
    });
    const host = new FakeHost({
      authenticated: true,
      productObservations: [observation],
    });
    renderApp(host);
    await navigateTo("Browse");
    fireEvent.click(await screen.findByRole("tab", { name: "Feedback" }));

    const row = await screen.findByRole("listitem", {
      name: `Product observation: ${observation.interpretation}`,
    });
    fireEvent.click(within(row).getByRole("button", { name: "Resolve" }));

    await waitFor(() => {
      expect(host.productObservations.resolveCalls).toEqual([
        { observationId: observation.id, version: 3 },
      ]);
    });
    await waitFor(() => {
      expect(
        screen.queryByRole("listitem", {
          name: `Product observation: ${observation.interpretation}`,
        }),
      ).not.toBeInTheDocument();
    });
  });
});
