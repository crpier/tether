import { expect, test } from "./fixtures";

function grantSuggestion(index: number) {
  const padded = index.toString().padStart(3, "0");
  return {
    approved: index,
    edited: 0,
    kind: `operation-${padded}`,
    last_rejection: null,
    rejected: 0,
    scope: `scope-${padded}`,
    seen: 201 - index,
  };
}

test("grants suggestions pagination is reflected in the URL", async ({
  page,
  login,
}) => {
  const suggestions = Array.from({ length: 200 }, (_, index) =>
    grantSuggestion(index + 1),
  );

  await page.route("**/api/proposals**", (route) =>
    route.fulfill({ contentType: "application/json", json: [] }),
  );
  await page.route("**/api/grants", (route) =>
    route.fulfill({ contentType: "application/json", json: [] }),
  );
  await page.route("**/api/grants/suggestions", (route) =>
    route.fulfill({ contentType: "application/json", json: suggestions }),
  );

  await login();
  await page.goto("/proposals?tab=grants", {
    waitUntil: "domcontentloaded",
  });

  const grantsPanel = page.getByRole("tabpanel", { name: /Grants/u });
  await expect(
    grantsPanel.getByRole("heading", { name: "Suggestions (200)" }),
  ).toBeVisible();
  await expect(grantsPanel.getByText("Page 1 of 8")).toBeVisible();
  await expect(page).toHaveURL(/\/proposals\?tab=grants$/u);

  await grantsPanel.getByRole("button", { name: "Next" }).click();
  await grantsPanel.getByRole("button", { name: "Next" }).click();

  await expect(grantsPanel.getByText("Page 3 of 8")).toBeVisible();
  await expect(
    grantsPanel.getByRole("button", { name: /^Grant operation-051/u }),
  ).toBeVisible();
  await expect(page).toHaveURL(/\/proposals\?tab=grants&page=3$/u);
});
