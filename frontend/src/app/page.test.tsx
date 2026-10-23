import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import Home from "./page";

describe("home page", () => {
  it("renders the current local workspace page", () => {
    render(<Home />);

    expect(screen.getByRole("main")).toBeTruthy();
    expect(
      screen.getByRole("heading", { level: 1, name: "Good Showroom AI" }),
    ).toBeTruthy();
    expect(
      screen.getByText(/Login and chat will be added in later tasks\./),
    ).toBeTruthy();
  });
});
