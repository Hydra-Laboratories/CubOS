import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import CameraPreview from "./CameraPreview";
import { containedImageRect, pointInImage } from "./cameraGeometry";

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

  it("computes the displayed image bounds for pillarboxing and letterboxing", () => {
    expect(containedImageRect(400, 300, 1600, 900)).toEqual({ left: 0, top: 37.5, width: 400, height: 225 });
    expect(containedImageRect(400, 300, 600, 1200)).toEqual({ left: 125, top: 0, width: 150, height: 300 });
  });

  it("maps clicks only inside displayed image pixels", () => {
    const image = { left: 0, top: 37.5, width: 400, height: 225 };
    expect(pointInImage(210, 170, 10, 20, image)).toEqual({ x: 0.5, y: 0.5 });
    expect(pointInImage(210, 30, 10, 20, image)).toBeNull();
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

  it("bounds the optical crosshair to the rendered image and proposes normalized centers without an action", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: string | URL | Request) =>
      String(input).includes("last-image") ? pngResponse() : jsonResponse({ image_path: "/tmp/frame.png" }),
    ));
    const proposed = vi.fn();
    const { container } = render(<CameraPreview instrument="camera" onProposedCenterChange={proposed} />);
    const image = await screen.findByAltText("Live preview from camera");
    const frame = screen.getByLabelText("Camera image review");
    Object.defineProperties(image, {
      naturalWidth: { configurable: true, value: 1600 },
      naturalHeight: { configurable: true, value: 900 },
    });
    vi.spyOn(frame, "getBoundingClientRect").mockReturnValue({
      left: 10, top: 20, width: 400, height: 300, right: 410, bottom: 320, x: 10, y: 20, toJSON: () => ({}),
    });
    fireEvent.load(image);

    const overlay = await screen.findByTestId("camera-image-overlay");
    expect(overlay).toHaveStyle({ left: "0px", top: "37.5px", width: "400px", height: "225px" });
    expect(container.querySelectorAll("svg line")).toHaveLength(2);

    fireEvent.click(frame, { clientX: 210, clientY: 30 });
    expect(proposed).not.toHaveBeenCalled();
    fireEvent.click(frame, { clientX: 210, clientY: 170 });
    expect(proposed).toHaveBeenCalledWith({ x: 0.5, y: 0.5 });
    expect(screen.getByRole("status")).toHaveTextContent("x 0.500, y 0.500 normalized");
    expect(screen.getByText(/does not move the gantry or save calibration/i)).toBeInTheDocument();
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
