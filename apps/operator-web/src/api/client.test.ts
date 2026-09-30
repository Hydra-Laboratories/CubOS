import { afterEach, describe, expect, it, vi } from "vitest";
import { dataApi, deckApi, settingsApi } from "./client";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("api client", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("throws parsed backend detail messages without raw JSON", async () => {
    vi.stubGlobal("fetch", vi.fn(async () =>
      jsonResponse({ detail: "config missing" }, 404),
    ));

    let caught: unknown;
    try {
      await deckApi.listConfigs();
    } catch (error) {
      caught = error;
    }

    expect(caught).toBeInstanceOf(Error);
    expect((caught as Error).message).toBe("config missing");
    expect(caught).toMatchObject({ name: "ApiError", status: 404 });
  });

  it("throws raw response text when no detail field is available", async () => {
    vi.stubGlobal("fetch", vi.fn(async () =>
      new Response("plain failure", {
        status: 500,
        statusText: "Internal Server Error",
      }),
    ));

    await expect(settingsApi.get()).rejects.toThrow("plain failure");
  });

  it("sends JSON content type on request bodies", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({ config_dir: "/mock/configs" }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await settingsApi.update("/mock/configs");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/settings",
      expect.objectContaining({
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ config_dir: "/mock/configs" }),
      }),
    );
  });

  it("downloads blobs on ok export responses", async () => {
    const fetchMock = vi.fn(async () =>
      new Response("zip bytes", {
        status: 200,
        headers: { "Content-Type": "application/zip" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const blob = await dataApi.downloadCampaignData(7);

    // Duck-type instead of instanceof: Response.blob() can return a Blob from
    // a different realm (Node vs jsdom global) depending on the Node version.
    expect(blob.size).toBe(9);
    expect(blob.type).toBe("application/zip");
    expect(await blob.text()).toBe("zip bytes");
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/data/campaigns/7/data.zip");
  });

  it("throws on failed downloads", async () => {
    vi.stubGlobal("fetch", vi.fn(async () =>
      new Response("zip unavailable", {
        status: 503,
        statusText: "Service Unavailable",
      }),
    ));

    await expect(dataApi.downloadCampaignData(7)).rejects.toThrow("zip unavailable");
  });
});
