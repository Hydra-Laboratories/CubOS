import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import CameraPreview from "./CameraPreview";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function pngResponse(): Response {
  const bytes = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  return new Response(bytes, { status: 200, headers: { "Content-Type": "image/png" } });
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

describe("CameraPreview", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows a loading state before the first frame arrives", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => undefined)));
    render(<CameraPreview instrument="camera" />);
    expect(screen.getByText("Loading preview…")).toBeInTheDocument();
  });

  it("always renders a centered crosshair overlay", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => undefined)));
    const { container } = render(<CameraPreview instrument="camera" />);
    const lines = container.querySelectorAll("svg line");
    expect(lines).toHaveLength(2);
  });

  it("captures with preview:true and displays the returned frame", async () => {
    const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes("/instruments/camera/capture")) {
        expect(JSON.parse(String(init?.body))).toMatchObject({ instrument: "camera", preview: true });
        return jsonResponse({ instrument: "camera", image_path: "/tmp/camera_preview.png" });
      }
      if (url.includes("/instruments/camera/last-image")) {
        return pngResponse();
      }
      throw new Error(`unexpected fetch ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<CameraPreview instrument="camera" />);

    const img = await screen.findByAltText("Live preview from camera");
    expect(img).toHaveAttribute("src", expect.stringMatching(/^blob:/));
  });

  it("shows an unavailable message and stops polling on a 501", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ detail: "Camera 'camera' does not support capture." }, 501));
    vi.stubGlobal("fetch", fetchMock);

    render(<CameraPreview instrument="camera" />);

    await screen.findByText(/does not support capture/i);
    const callsAfterFirstFailure = fetchMock.mock.calls.length;

    await delay(1200);

    expect(fetchMock.mock.calls.length).toBe(callsAfterFirstFailure);
  }, 10000);

  it("keeps polling and shows an error banner on a transient failure", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ detail: "Capture failed: camera busy" }, 502));
    vi.stubGlobal("fetch", fetchMock);

    render(<CameraPreview instrument="camera" />);

    await screen.findByText(/camera busy/i);
    const callsAfterFirstFailure = fetchMock.mock.calls.length;

    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(callsAfterFirstFailure), { timeout: 3000 });
  }, 10000);
});
