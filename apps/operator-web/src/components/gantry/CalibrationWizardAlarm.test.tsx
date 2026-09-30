import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import CalibrationWizard from "./CalibrationWizard";
import type { GantryConfig, GantryPosition } from "../../types";

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function deferredResponse() {
  let resolve!: (value: Response) => void;
  const promise = new Promise<Response>((innerResolve) => {
    resolve = innerResolve;
  });
  return { promise, resolve };
}

function gantryConfig(): GantryConfig {
  return {
    serial_port: "/dev/ttyUSB0",
    gantry_type: "cub_xl",
    cnc: {
      factory_z_travel_mm: 110,
      calibration_block_height_mm: 35,
      y_axis_motion: "head",
      safe_z: 110,
    },
    working_volume: {
      x_min: 0,
      x_max: 400,
      y_min: 0,
      y_max: 300,
      z_min: 0,
      z_max: 110,
    },
    grbl_settings: {},
    instruments: {
      asmi: {
        type: "asmi",
        vendor: "vernier",
        offset_x: 0,
        offset_y: 0,
        depth: 0,
      },
    },
  };
}

function position(status = "Idle"): GantryPosition {
  return {
    x: 0,
    y: 0,
    z: 20,
    work_x: 0,
    work_y: 0,
    work_z: 20,
    status,
    connected: true,
    calibration_active: false,
  };
}

async function advanceToOriginJog(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Continue" }));
  await user.click(await screen.findByRole("button", { name: "Home gantry" }));
  await user.click(await screen.findByRole("button", { name: "Continue" }));
  return screen.findByRole("button", { name: "Z-" });
}

describe("CalibrationWizard alarm recovery", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows the power-cycle instruction when recovery reports connection loss", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = new URL(typeof input === "string" ? input : (input as Request).url, "http://localhost");
      if (url.pathname === "/api/v1/gantry/jog" && init?.method === "POST") {
        return new Response(JSON.stringify({ detail: "ALARM:1" }), {
          status: 409,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.pathname === "/api/v1/gantry/calibration/recover-limit" && init?.method === "POST") {
        return new Response(
          JSON.stringify({
            detail:
              "Controller connection lost during limit recovery. Power-cycle the controller, re-seat the USB cable, then Disconnect and Connect again before continuing.",
          }),
          { status: 503, headers: { "Content-Type": "application/json" } },
        );
      }
      return jsonResponse(position());
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <CalibrationWizard
        open
        onClose={() => undefined}
        gantry={{ filename: "cubos.yaml", config: gantryConfig() }}
        position={position()}
        onSaveCalibrated={async () => undefined}
      />,
    );

    const zDown = await advanceToOriginJog(user);
    await user.click(zDown);

    // The recovery failure surfaces on both the primary "GANTRY ALARM" banner
    // and the secondary generic error line, so a bare findByText on this
    // substring matches two elements. Scope to the alarm banner, which is
    // the actionable instruction this test is actually about.
    const alarmBanner = (await screen.findByText("GANTRY ALARM")).closest("div");
    expect(alarmBanner).toHaveTextContent(/Power-cycle the controller, re-seat the USB cable/);
  });

  it("closes even when restoring soft limits fails (dead controller link)", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const fetchMock = vi.fn(async (input: string | URL | Request) => {
      const url = new URL(typeof input === "string" ? input : (input as Request).url, "http://localhost");
      if (url.pathname === "/api/v1/gantry/calibration/restore-soft-limits") {
        return new Response(JSON.stringify({ detail: "serial read timed out" }), {
          status: 500,
          headers: { "Content-Type": "application/json" },
        });
      }
      return jsonResponse(position());
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <CalibrationWizard
        open
        onClose={onClose}
        gantry={{ filename: "cubos.yaml", config: gantryConfig() }}
        position={position()}
        onSaveCalibrated={async () => undefined}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Close calibration" }));

    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it("resets the wizard and surfaces the error when restore fails", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async (input: string | URL | Request) => {
      const url = new URL(typeof input === "string" ? input : (input as Request).url, "http://localhost");
      if (url.pathname === "/api/v1/gantry/calibration/restore-soft-limits") {
        return new Response(JSON.stringify({ detail: "serial read timed out" }), {
          status: 500,
          headers: { "Content-Type": "application/json" },
        });
      }
      return jsonResponse(position());
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <CalibrationWizard
        open
        onClose={() => undefined}
        gantry={{ filename: "cubos.yaml", config: gantryConfig() }}
        position={position()}
        onSaveCalibrated={async () => undefined}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Reset wizard" }));

    expect(await screen.findByText(/Failed to restore soft limits/)).toBeInTheDocument();
    // The wizard still reset to the first step instead of staying trapped.
    expect(document.querySelector('[aria-current="step"]')?.textContent).toContain("Prepare");
  });

  it("tells the operator to jog in small steps near the edges of travel", () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(position())));

    render(
      <CalibrationWizard
        open
        onClose={() => undefined}
        gantry={{ filename: "cubos.yaml", config: gantryConfig() }}
        position={position()}
        onSaveCalibrated={async () => undefined}
      />,
    );

    expect(screen.getByText(/Jog in small steps near the edges of travel/)).toBeInTheDocument();
  });

  it("automatically recovers and locks controls when Z jog hits a limit", async () => {
    const user = userEvent.setup();
    const recovery = deferredResponse();
    const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = new URL(
        typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url,
        "http://localhost",
      );
      if (url.pathname === "/api/v1/gantry/calibration/prepare-origin" && init?.method === "POST") {
        return jsonResponse(position());
      }
      if (url.pathname === "/api/v1/gantry/jog" && init?.method === "POST") {
        return new Response("ALARM:1 hard limit", { status: 409 });
      }
      if (url.pathname === "/api/v1/gantry/calibration/recover-limit" && init?.method === "POST") {
        return recovery.promise;
      }
      return jsonResponse(position());
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <CalibrationWizard
        open
        onClose={() => undefined}
        gantry={{ filename: "cubos.yaml", config: gantryConfig() }}
        position={position()}
        onSaveCalibrated={async () => undefined}
      />,
    );

    const zDown = await advanceToOriginJog(user);

    await user.click(zDown);

    expect(await screen.findByText("GANTRY ALARM")).toBeInTheDocument();
    expect(zDown).toBeDisabled();

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/gantry/calibration/recover-limit",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ x: 0, y: 0, z: -0.5 }),
      }),
    ));

    recovery.resolve(jsonResponse({
      status: "recovered",
      attempts: 1,
      pull_off: { x: 0, y: 0, z: 5 },
      messages: ["recovered"],
    }));

    await waitFor(() => expect(screen.queryByText("GANTRY ALARM")).not.toBeInTheDocument());
    expect(await screen.findByText(/Backed off the limit switch/)).toBeInTheDocument();
    expect(zDown).not.toBeDisabled();
  });

  it("recovers when a hard limit is first reported by position polling after a jog", async () => {
    const user = userEvent.setup();
    const recovery = deferredResponse();
    const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = new URL(
        typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url,
        "http://localhost",
      );
      if (url.pathname === "/api/v1/gantry/calibration/prepare-origin" && init?.method === "POST") {
        return jsonResponse(position());
      }
      if (url.pathname === "/api/v1/gantry/jog" && init?.method === "POST") {
        return jsonResponse({ status: "ok" });
      }
      if (url.pathname === "/api/v1/gantry/calibration/recover-limit" && init?.method === "POST") {
        return recovery.promise;
      }
      return jsonResponse(position());
    });
    vi.stubGlobal("fetch", fetchMock);

    const props = {
      open: true,
      onClose: () => undefined,
      gantry: { filename: "cubos.yaml", config: gantryConfig() },
      onSaveCalibrated: async () => undefined,
    };
    const { rerender } = render(
      <CalibrationWizard
        {...props}
        position={position()}
      />,
    );

    await user.click(await advanceToOriginJog(user));

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/gantry/jog",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ x: 0, y: 0, z: -0.5 }),
      }),
    );

    rerender(
      <CalibrationWizard
        {...props}
        position={position("ALARM:1")}
      />,
    );

    expect(await screen.findByText("GANTRY ALARM")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Z-" })).toBeDisabled();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/gantry/calibration/recover-limit",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ x: 0, y: 0, z: -0.5 }),
      }),
    ));

    recovery.resolve(jsonResponse({
      status: "recovered",
      attempts: 1,
      pull_off: { x: 0, y: 0, z: 5 },
      messages: ["recovered"],
    }));

    await waitFor(() => expect(screen.queryByText("GANTRY ALARM")).not.toBeInTheDocument());
    expect(await screen.findByText(/Backed off the limit switch/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Z-" })).not.toBeDisabled();
  });

  it("does not auto-recover a poll alarm during a blocking action with a stale jog delta", async () => {
    const user = userEvent.setup();
    const restore = deferredResponse();
    let recoverCallCount = 0;
    const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = new URL(
        typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url,
        "http://localhost",
      );
      if (url.pathname === "/api/v1/gantry/calibration/prepare-origin" && init?.method === "POST") {
        return jsonResponse(position());
      }
      if (url.pathname === "/api/v1/gantry/jog" && init?.method === "POST") {
        return jsonResponse({ status: "ok" });
      }
      if (url.pathname === "/api/v1/gantry/position") {
        return jsonResponse(position());
      }
      if (url.pathname === "/api/v1/gantry/work-coordinates" && init?.method === "POST") {
        return jsonResponse(position());
      }
      if (url.pathname === "/api/v1/gantry/calibration/restore-soft-limits" && init?.method === "POST") {
        return restore.promise;
      }
      if (url.pathname === "/api/v1/gantry/calibration/recover-limit" && init?.method === "POST") {
        recoverCallCount++;
        return jsonResponse({
          status: "recovered",
          attempts: 1,
          pull_off: { x: 0, y: 0, z: 5 },
          messages: ["recovered"],
        });
      }
      return jsonResponse(position());
    });
    vi.stubGlobal("fetch", fetchMock);

    const props = {
      open: true,
      onClose: () => undefined,
      gantry: { filename: "cubos.yaml", config: gantryConfig() },
      onSaveCalibrated: async () => undefined,
    };
    const { rerender } = render(<CalibrationWizard {...props} position={position()} />);

    const zDown = await advanceToOriginJog(user);
    await user.click(zDown);
    await user.click(screen.getByRole("button", { name: "Set origin and continue" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/gantry/calibration/restore-soft-limits",
      expect.objectContaining({ method: "POST" }),
    ));

    rerender(<CalibrationWizard {...props} position={position("ALARM:1")} />);
    await new Promise((r) => setTimeout(r, 50));
    expect(recoverCallCount).toBe(0);

    restore.resolve(jsonResponse(position()));

    expect(await screen.findByText(/cannot back off on its own/i)).toBeInTheDocument();
    expect(recoverCallCount).toBe(0);
  });

  it("recovers when a reset-to-continue error fires during a jog", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = new URL(
        typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url,
        "http://localhost",
      );
      if (url.pathname === "/api/v1/gantry/calibration/prepare-origin" && init?.method === "POST") {
        return jsonResponse(position());
      }
      if (url.pathname === "/api/v1/gantry/jog" && init?.method === "POST") {
        return new Response("error:9 Reset to continue", { status: 409 });
      }
      if (url.pathname === "/api/v1/gantry/calibration/recover-limit" && init?.method === "POST") {
        return jsonResponse({
          status: "recovered",
          attempts: 2,
          pull_off: { x: 0, y: 0, z: 5 },
          messages: ["recovered"],
        });
      }
      return jsonResponse(position());
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <CalibrationWizard
        open
        onClose={() => undefined}
        gantry={{ filename: "cubos.yaml", config: gantryConfig() }}
        position={position()}
        onSaveCalibrated={async () => undefined}
      />,
    );

    const zDown = await advanceToOriginJog(user);

    await user.click(zDown);

    expect(await screen.findByText(/Backed off the limit switch/)).toBeInTheDocument();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/gantry/calibration/recover-limit",
      expect.objectContaining({ method: "POST" }),
    ));
    expect(zDown).not.toBeDisabled();
  });

  it("keeps controls locked when automatic recovery cannot clear the alarm", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = new URL(
        typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url,
        "http://localhost",
      );
      if (url.pathname === "/api/v1/gantry/calibration/prepare-origin" && init?.method === "POST") {
        return jsonResponse(position());
      }
      if (url.pathname === "/api/v1/gantry/jog" && init?.method === "POST") {
        return new Response("ALARM:1 hard limit", { status: 409 });
      }
      if (url.pathname === "/api/v1/gantry/calibration/recover-limit" && init?.method === "POST") {
        return new Response("still on limit", { status: 409 });
      }
      return jsonResponse(position());
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <CalibrationWizard
        open
        onClose={() => undefined}
        gantry={{ filename: "cubos.yaml", config: gantryConfig() }}
        position={position()}
        onSaveCalibrated={async () => undefined}
      />,
    );

    const zDown = await advanceToOriginJog(user);

    await user.click(zDown);

    expect(await screen.findByText("GANTRY ALARM")).toBeInTheDocument();
    expect(await screen.findByText(/Could not back off the limit switch/)).toBeInTheDocument();
    expect(await screen.findByText(/Limit recovery failed after jog error/)).toBeInTheDocument();
    expect(zDown).toBeDisabled();
  });

  it("recovers when position polling reports a Pn: active limit pin status", async () => {
    const user = userEvent.setup();
    const recovery = deferredResponse();
    const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = new URL(
        typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url,
        "http://localhost",
      );
      if (url.pathname === "/api/v1/gantry/calibration/prepare-origin" && init?.method === "POST") {
        return jsonResponse(position());
      }
      if (url.pathname === "/api/v1/gantry/jog" && init?.method === "POST") {
        return jsonResponse({ status: "ok" });
      }
      if (url.pathname === "/api/v1/gantry/calibration/recover-limit" && init?.method === "POST") {
        return recovery.promise;
      }
      return jsonResponse(position());
    });
    vi.stubGlobal("fetch", fetchMock);

    const props = {
      open: true,
      onClose: () => undefined,
      gantry: { filename: "cubos.yaml", config: gantryConfig() },
      onSaveCalibrated: async () => undefined,
    };
    const { rerender } = render(<CalibrationWizard {...props} position={position()} />);

    await user.click(await advanceToOriginJog(user));

    rerender(<CalibrationWizard {...props} position={position("<Idle|WPos:0.0,0.0,20.0|Pn:Z>")} />);

    expect(await screen.findByText("GANTRY ALARM")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Z-" })).toBeDisabled();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/gantry/calibration/recover-limit",
      expect.objectContaining({ method: "POST" }),
    ));

    recovery.resolve(jsonResponse({
      status: "recovered",
      attempts: 1,
      pull_off: { x: 0, y: 0, z: 5 },
      messages: ["recovered"],
    }));

    await waitFor(() => expect(screen.queryByText("GANTRY ALARM")).not.toBeInTheDocument());
  });

  it("does not re-trigger recovery when re-rendered with the same alarm status and delta", async () => {
    const user = userEvent.setup();
    let recoverCallCount = 0;
    const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = new URL(
        typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url,
        "http://localhost",
      );
      if (url.pathname === "/api/v1/gantry/calibration/prepare-origin" && init?.method === "POST") {
        return jsonResponse(position());
      }
      if (url.pathname === "/api/v1/gantry/jog" && init?.method === "POST") {
        return jsonResponse({ status: "ok" });
      }
      if (url.pathname === "/api/v1/gantry/calibration/recover-limit" && init?.method === "POST") {
        recoverCallCount++;
        return jsonResponse({
          status: "recovered",
          attempts: 1,
          pull_off: { x: 0, y: 0, z: 5 },
          messages: ["recovered"],
        });
      }
      return jsonResponse(position());
    });
    vi.stubGlobal("fetch", fetchMock);

    const props = {
      open: true,
      onClose: () => undefined,
      gantry: { filename: "cubos.yaml", config: gantryConfig() },
      onSaveCalibrated: async () => undefined,
    };
    const { rerender } = render(<CalibrationWizard {...props} position={position()} />);

    await user.click(await advanceToOriginJog(user));

    // First alarm appearance triggers recovery.
    rerender(<CalibrationWizard {...props} position={position("ALARM:1")} />);
    await waitFor(() => expect(recoverCallCount).toBe(1));

    // Re-rendering with the same status and delta must not fire a second call.
    rerender(<CalibrationWizard {...props} position={position("ALARM:1")} />);
    await new Promise((r) => setTimeout(r, 100));
    expect(recoverCallCount).toBe(1);
  });

  it("stops jog repeats and shows error when a non-alarm jog error fires", async () => {
    const user = userEvent.setup({ delay: null });
    let jogCallCount = 0;
    const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = new URL(
        typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url,
        "http://localhost",
      );
      if (url.pathname === "/api/v1/gantry/calibration/prepare-origin" && init?.method === "POST") {
        return jsonResponse(position());
      }
      if (url.pathname === "/api/v1/gantry/jog" && init?.method === "POST") {
        jogCallCount++;
        return new Response("serial port timed out", { status: 500 });
      }
      return jsonResponse(position());
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <CalibrationWizard
        open
        onClose={() => undefined}
        gantry={{ filename: "cubos.yaml", config: gantryConfig() }}
        position={position()}
        onSaveCalibrated={async () => undefined}
      />,
    );

    const zDown = await advanceToOriginJog(user);
    await user.click(zDown);

    await waitFor(() => expect(screen.queryByText(/serial port timed out/i)).toBeInTheDocument());
    expect(screen.queryByText("GANTRY ALARM")).not.toBeInTheDocument();

    const countAfterError = jogCallCount;
    await new Promise((r) => setTimeout(r, 300));
    expect(jogCallCount).toBe(countAfterError);
  });
});
