import { render, screen } from "@testing-library/react";
import { getServerSession } from "next-auth";
import { describe, expect, it, vi } from "vitest";
import Home from "./page";

vi.mock("next-auth", () => ({ getServerSession: vi.fn() }));
vi.mock("../lib/server/auth", () => ({ configuredAuthOptions: () => ({}) }));

describe("home page", () => {
  it("renders a sign-in link when the user is signed out", async () => {
    vi.mocked(getServerSession).mockResolvedValueOnce(null);
    render(await Home());

    expect(screen.getByRole("main")).toBeTruthy();
    expect(
      screen.getByRole("heading", { level: 1, name: "Good Showroom AI" }),
    ).toBeTruthy();
    expect(screen.getByRole("link", { name: "Sign in" }).getAttribute("href")).toBe(
      "/api/auth/signin",
    );
    expect(screen.queryByRole("link", { name: "Sign out" })).toBeNull();
  });

  it("links a signed-in user to the protected sign-out confirmation", async () => {
    vi.mocked(getServerSession).mockResolvedValueOnce({
      expires: "2026-11-23T12:00:00.000Z",
    });
    render(await Home());

    expect(screen.getByRole("link", { name: "Sign out" }).getAttribute("href")).toBe(
      "/api/auth/signout",
    );
    expect(screen.queryByRole("link", { name: "Sign in" })).toBeNull();
  });
});
