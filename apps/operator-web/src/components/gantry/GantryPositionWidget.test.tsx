import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import GantryPositionWidget from "./GantryPositionWidget";
import type { GantryConfig, GantryPosition, WorkingVolume } from "../../types";

function position(overrides: Partial<GantryPosition> = {}): GantryPosition {
  return {
    x: 0,
    y: 0,
    z: 0,
    work_x: 0,
    work_y: 0,
    work_z: 0,
    status: "Idle",
    connected: true,
    calibration_active: false,
    ...overrides,
  };
}

const workingVolume: WorkingVolume = {
  x_min: 0,
  x_max: 300,
  y_min: 0,
  y_max: 200,
  z_min: 0,
  z_max: 80,
};

// home_origin fixture: WPos zero at the homed back-right-top corner, so the
// entire reachable volume is negative (see calibrationMath.buildCalibratedConfig).
const negativeWorkingVolume: WorkingVolume = {
  x_min: -400,
  x_max: 0,
  y_min: -300,
  y_max: 0,
  z_min: -100,
  z_max: 0,
};

function gantryConfig(): GantryConfig {
  return {
    serial_port: "/dev/ttyUSB0",
    gantry_type: "cub_xl",
    cnc: {
      factory_z_travel_mm: 80,
      calibration_block_height_mm: 35,
      y_axis_motion: "head",
      safe_z: 80,
    },
    working_volume: workingVolume,
    grbl_settings: {},
    instruments: {
      pipette_1: {
        type: "pipette",
        vendor: "opentrons",
        offset_x: 0,
        offset_y: 0,
        depth: 0,
      },
    },
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function requestPath(input: string | URL | Request): string {
  return new URL(
    typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url,
    "http://localhost",
  ).pathname;
}

describe("GantryPositionWidget manual move safety", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("sends the move when workingVolume is absent (backend is the guard)", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ x: 0, y: 0, z: 0, status: "Idle", connected: true, calibration_active: false }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(
      <GantryPositionWidget
        position={position()}
        workingVolume={null}
        gantryFile="cubos.yaml"
        gantry={null}
        onSaveCalibrated={async () => undefined}
      />,
    );

    await user.type(screen.getByLabelText("X (mm)"), "999");
    await user.type(screen.getByLabelText("Y (mm)"), "999");
    await user.type(screen.getByLabelText("Z (mm)"), "999");
    await user.click(screen.getByRole("button", { name: "Go" }));

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/gantry/move-to",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ x: 999, y: 999, z: 999 }),
      }),
    );
  });

  it("shows connection failures and returns the Connect button to ready state", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async () =>
      jsonResponse({ detail: "serial port unavailable" }, 500),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(
      <GantryPositionWidget
        position={position({ connected: false })}
        workingVolume={workingVolume}
        gantryFile="cubos.yaml"
        gantry={null}
        onSaveCalibrated={async () => undefined}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Connect" }));

    expect(await screen.findByText(/Connection failed/)).toHaveTextContent("serial port unavailable");
    expect(screen.getByRole("button", { name: "Connect" })).toBeEnabled();
    // A failed connect must not offer to home.
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("prompts to home after a successful connect and homes on accept", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async () => jsonResponse({ status: "ok" }));
    vi.stubGlobal("fetch", fetchMock);

    render(
      <GantryPositionWidget
        position={position({ connected: false })}
        workingVolume={workingVolume}
        gantryFile="cubos.yaml"
        gantry={null}
        onSaveCalibrated={async () => undefined}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Connect" }));

    const dialog = await screen.findByRole("alertdialog", { name: "Gantry connected" });
    expect(dialog).toHaveTextContent("Home the gantry now?");
    expect(fetchMock).not.toHaveBeenCalledWith(
      "/api/v1/gantry/home",
      expect.objectContaining({ method: "POST" }),
    );

    await user.click(screen.getByRole("button", { name: "Home now" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/gantry/home",
      expect.objectContaining({ method: "POST" }),
    ));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("does not home when the post-connect prompt is declined", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async () => jsonResponse({ status: "ok" }));
    vi.stubGlobal("fetch", fetchMock);

    render(
      <GantryPositionWidget
        position={position({ connected: false })}
        workingVolume={workingVolume}
        gantryFile="cubos.yaml"
        gantry={null}
        onSaveCalibrated={async () => undefined}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Connect" }));
    await user.click(await screen.findByRole("button", { name: "Not now" }));

    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalledWith(
      "/api/v1/gantry/home",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("reads GRBL settings and displays them in numeric setting order", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async (input: string | URL | Request) => {
      if (requestPath(input) === "/api/v1/gantry/grbl-settings") {
        return jsonResponse({ settings: { "$20": "1", "$3": "5" } });
      }
      return jsonResponse({ status: "ok" });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <GantryPositionWidget
        position={position()}
        workingVolume={workingVolume}
        gantryFile="cubos.yaml"
        gantry={null}
        onSaveCalibrated={async () => undefined}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Advanced" }));
    await user.click(screen.getByRole("button", { name: "Read GRBL Settings" }));

    const setting3 = await screen.findByText("$3");
    const setting20 = screen.getByText("$20");
    expect(setting3.compareDocumentPosition(setting20) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(setting3.closest("div")).toHaveTextContent("5");
    expect(setting20.closest("div")).toHaveTextContent("1");
  });

  it("sends a GRBL setting update payload", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async () =>
      jsonResponse({ settings: { "$20": "1" } }),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(
      <GantryPositionWidget
        position={position()}
        workingVolume={workingVolume}
        gantryFile="cubos.yaml"
        gantry={null}
        onSaveCalibrated={async () => undefined}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Advanced" }));
    await user.clear(screen.getByLabelText("Setting"));
    await user.type(screen.getByLabelText("Setting"), "$20");
    await user.type(screen.getByLabelText("Value"), "1");
    await user.click(screen.getByRole("button", { name: "Send Setting" }));

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/gantry/grbl-settings",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ setting: "$20", value: "1" }),
      }),
    );
  });

  it("shows the ALARM banner and posts unlock", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async () =>
      jsonResponse(position({ status: "Idle" })),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(
      <GantryPositionWidget
        position={position({ status: "ALARM:1" })}
        workingVolume={workingVolume}
        gantryFile="cubos.yaml"
        gantry={null}
        onSaveCalibrated={async () => undefined}
      />,
    );

    expect(screen.getByText("ALARM")).toBeInTheDocument();
    expect(screen.getAllByText(/ALARM:1/).length).toBeGreaterThan(0);

    await user.click(screen.getByRole("button", { name: "Unlock ($X)" }));

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/gantry/unlock",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("offers Pull off limit after a jog and posts limit recovery", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ status: "ok" }));
    vi.stubGlobal("fetch", fetchMock);

    const widget = (pos: GantryPosition) => (
      <GantryPositionWidget
        position={pos}
        workingVolume={workingVolume}
        gantryFile="cubos.yaml"
        gantry={null}
        onSaveCalibrated={async () => undefined}
      />
    );
    const { rerender } = render(widget(position()));

    fireEvent.mouseDown(screen.getByTitle("X+"));
    fireEvent.mouseUp(screen.getByTitle("X+"));

    rerender(widget(position({ status: "ALARM:1" })));

    fireEvent.click(screen.getByRole("button", { name: "Pull off limit" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/v1/gantry/calibration/recover-limit",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ x: 0.5, y: 0, z: 0 }),
        }),
      );
    });
  });

  it("hides Pull off limit when no jog direction is known", () => {
    const fetchMock = vi.fn(async () => jsonResponse({ status: "ok" }));
    vi.stubGlobal("fetch", fetchMock);

    render(
      <GantryPositionWidget
        position={position({ status: "ALARM:1" })}
        workingVolume={workingVolume}
        gantryFile="cubos.yaml"
        gantry={null}
        onSaveCalibrated={async () => undefined}
      />,
    );

    expect(screen.queryByRole("button", { name: "Pull off limit" })).toBeNull();
    expect(screen.getByRole("button", { name: "Unlock ($X)" })).toBeInTheDocument();
  });

  it("hides Pull off limit for non-limit alarms even with a known jog direction", () => {
    const fetchMock = vi.fn(async () => jsonResponse({ status: "ok" }));
    vi.stubGlobal("fetch", fetchMock);

    const widget = (pos: GantryPosition) => (
      <GantryPositionWidget
        position={pos}
        workingVolume={workingVolume}
        gantryFile="cubos.yaml"
        gantry={null}
        onSaveCalibrated={async () => undefined}
      />
    );
    const { rerender } = render(widget(position()));

    fireEvent.mouseDown(screen.getByTitle("X+"));
    fireEvent.mouseUp(screen.getByTitle("X+"));

    // ALARM:3 is abort-during-cycle (e-stop / reset) — the gantry is not on
    // a limit switch, so an automatic 5 mm pull-off move must not be offered.
    rerender(widget(position({ status: "ALARM:3" })));

    expect(screen.queryByRole("button", { name: "Pull off limit" })).toBeNull();
    expect(screen.getByRole("button", { name: "Unlock ($X)" })).toBeInTheDocument();
  });

  it("re-paces to the new segment on a direction change mid-hold", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn<(input: string | URL | Request, init?: RequestInit) => Promise<Response>>(
      async (input) => {
        if (requestPath(input) === "/api/v1/gantry/jog-cancel") {
          return jsonResponse(position());
        }
        return jsonResponse({ status: "ok" });
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    render(
      <GantryPositionWidget
        position={position()}
        workingVolume={workingVolume}
        gantryFile="cubos.yaml"
        gantry={null}
        onSaveCalibrated={async () => undefined}
      />,
    );

    const jogBodies = () =>
      fetchMock.mock.calls
        .filter(([input]) => requestPath(input) === "/api/v1/gantry/jog")
        .map(([, init]) => JSON.parse(String(init?.body)));

    // Hold Z at the default 0.5 mm step (150 ms pace), then press X with a
    // 20 mm step (480 ms pace) without releasing. The repeats must follow
    // the 20 mm segment's pace — 20 mm jogs fired at the 0.5 mm cadence is
    // exactly the backlog overrun this pacing exists to prevent.
    fireEvent.change(screen.getByLabelText("XY mm"), { target: { value: "20" } });
    fireEvent.mouseDown(screen.getByTitle("Z+"));

    await vi.advanceTimersByTimeAsync(100);
    expect(jogBodies()).toEqual([{ x: 0, y: 0, z: 0.5 }]);

    fireEvent.mouseDown(screen.getByTitle("X+"));
    await vi.advanceTimersByTimeAsync(10);
    expect(jogBodies()).toEqual([
      { x: 0, y: 0, z: 0.5 },
      { x: 20, y: 0, z: 0 },
    ]);

    // Well past the old 150 ms cadence but before the 20 mm pace elapses:
    // no further sends.
    await vi.advanceTimersByTimeAsync(440);
    expect(jogBodies()).toHaveLength(2);

    // After the 20 mm pace (480 ms from its send) the next repeat fires.
    await vi.advanceTimersByTimeAsync(100);
    expect(jogBodies()).toHaveLength(3);

    fireEvent.mouseUp(screen.getByTitle("X+"));
    await vi.advanceTimersByTimeAsync(0);
    expect(
      fetchMock.mock.calls.some(([input]) => requestPath(input) === "/api/v1/gantry/jog-cancel"),
    ).toBe(true);
    vi.useRealTimers();
  });

  it("paces held jog repeats to the segment execution time", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(async (input: string | URL | Request) => {
      if (requestPath(input) === "/api/v1/gantry/jog-cancel") {
        return jsonResponse(position());
      }
      return jsonResponse({ status: "ok" });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <GantryPositionWidget
        position={position()}
        workingVolume={workingVolume}
        gantryFile="cubos.yaml"
        gantry={null}
        onSaveCalibrated={async () => undefined}
      />,
    );

    const jogCalls = () =>
      fetchMock.mock.calls.filter(([input]) => requestPath(input) === "/api/v1/gantry/jog");

    // A 20 mm step at the 2000 mm/min jog feed takes 600 ms to execute, so
    // held repeats pace to 0.8x that (480 ms), not the 150 ms UI tick —
    // otherwise GRBL queues a backlog that keeps moving after release.
    fireEvent.change(screen.getByLabelText("XY mm"), { target: { value: "20" } });
    fireEvent.mouseDown(screen.getByTitle("X+"));

    await vi.advanceTimersByTimeAsync(400);
    expect(jogCalls()).toHaveLength(1);

    await vi.advanceTimersByTimeAsync(200);
    expect(jogCalls()).toHaveLength(2);

    fireEvent.mouseUp(screen.getByTitle("X+"));
    await vi.advanceTimersByTimeAsync(0);
    expect(
      fetchMock.mock.calls.some(([input]) => requestPath(input) === "/api/v1/gantry/jog-cancel"),
    ).toBe(true);
    vi.useRealTimers();
  });

  it("runs advanced machine commands and shows success messages", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async () => jsonResponse(position()));
    vi.stubGlobal("fetch", fetchMock);

    render(
      <GantryPositionWidget
        position={position()}
        workingVolume={workingVolume}
        gantryFile="cubos.yaml"
        gantry={null}
        onSaveCalibrated={async () => undefined}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Advanced" }));

    await user.click(screen.getByRole("button", { name: "Clear Alarm" }));
    expect(await screen.findByText("Unlock sent.")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/gantry/unlock", expect.objectContaining({ method: "POST" }));

    await user.click(screen.getByRole("button", { name: "Reset + Unlock" }));
    expect(await screen.findByText("Reset and unlock sent.")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/gantry/reset-unlock", expect.objectContaining({ method: "POST" }));

    await user.click(screen.getByRole("button", { name: "Feed Hold" }));
    expect(await screen.findByText("Feed hold sent.")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/gantry/feed-hold", expect.objectContaining({ method: "POST" }));

    await user.click(screen.getByRole("button", { name: "Resume" }));
    expect(await screen.findByText("Resume sent; verify Idle before homing.")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/gantry/resume", expect.objectContaining({ method: "POST" }));

    await user.click(screen.getByRole("button", { name: "Cancel Jog" }));
    expect(await screen.findByText("Jog cancel sent.")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/gantry/jog-cancel", expect.objectContaining({ method: "POST" }));
  });

  it("blocks Home during feed hold and shows explicit recovery guidance", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async () => jsonResponse(position({ status: "Idle" })));
    vi.stubGlobal("fetch", fetchMock);

    render(
      <GantryPositionWidget
        position={position({ status: "Hold:0" })}
        workingVolume={workingVolume}
        gantryFile="cubos.yaml"
        gantry={null}
        onSaveCalibrated={async () => undefined}
      />,
    );

    expect(screen.getByRole("button", { name: "Home" })).toBeDisabled();
    expect(screen.getByText(/Feed hold is active/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Resume" }));

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/gantry/resume",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("shows disconnect failures and returns the Disconnect button to ready state", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async () =>
      jsonResponse({ detail: "serial close failed" }, 500),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(
      <GantryPositionWidget
        position={position()}
        workingVolume={workingVolume}
        gantryFile="cubos.yaml"
        gantry={null}
        onSaveCalibrated={async () => undefined}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Disconnect" }));

    expect(await screen.findByText(/Disconnect failed/)).toHaveTextContent("serial close failed");
    expect(screen.getByRole("button", { name: "Disconnect" })).toBeEnabled();
  });

  it("confirms and sends home", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async () =>
      jsonResponse(position({ x: 300, y: 200, z: 80 })),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(
      <GantryPositionWidget
        position={position()}
        workingVolume={workingVolume}
        gantryFile="cubos.yaml"
        gantry={null}
        onSaveCalibrated={async () => undefined}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Home" }));

    // Homing is gated on the in-app confirm dialog (window.confirm is
    // silently auto-dismissed in embedded browser panes).
    const dialog = await screen.findByRole("alertdialog", { name: "Home gantry" });
    expect(dialog).toHaveTextContent("Confirm you want to go to home?");
    expect(fetchMock).not.toHaveBeenCalledWith(
      "/api/v1/gantry/home",
      expect.objectContaining({ method: "POST" }),
    );

    await user.click(screen.getByRole("button", { name: "Go to home" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/gantry/home",
      expect.objectContaining({ method: "POST" }),
    ));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("does not home when the confirm dialog is cancelled", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async () => jsonResponse(position()));
    vi.stubGlobal("fetch", fetchMock);

    render(
      <GantryPositionWidget
        position={position()}
        workingVolume={workingVolume}
        gantryFile="cubos.yaml"
        gantry={null}
        onSaveCalibrated={async () => undefined}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Home" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalledWith(
      "/api/v1/gantry/home",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("renders incoming move errors as dismissible command errors", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    render(
      <GantryPositionWidget
        position={position({ move_error: "Soft limit hit" })}
        workingVolume={workingVolume}
        gantryFile="cubos.yaml"
        gantry={null}
        onSaveCalibrated={async () => undefined}
      />,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent("Soft limit hit");
    await user.click(screen.getByRole("button", { name: "Dismiss command error" }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("does not send a manual move outside the working volume", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    render(
      <GantryPositionWidget
        position={position()}
        workingVolume={workingVolume}
        gantryFile="cubos.yaml"
        gantry={null}
        onSaveCalibrated={async () => undefined}
      />,
    );

    await user.type(screen.getByLabelText("X (mm)"), "301");
    await user.type(screen.getByLabelText("Y (mm)"), "100");
    await user.type(screen.getByLabelText("Z (mm)"), "40");
    await user.click(screen.getByRole("button", { name: "Go" }));

    expect(await screen.findByText(/Move target outside working volume/)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("validates manual move coordinates before sending", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    render(
      <GantryPositionWidget
        position={position()}
        workingVolume={workingVolume}
        gantryFile="cubos.yaml"
        gantry={null}
        onSaveCalibrated={async () => undefined}
      />,
    );

    await user.type(screen.getByLabelText("X (mm)"), "abc");
    await user.type(screen.getByLabelText("Y (mm)"), "10");
    await user.type(screen.getByLabelText("Z (mm)"), "10");
    await user.click(screen.getByRole("button", { name: "Go" }));

    expect(await screen.findByText("Enter valid X, Y, and Z coordinates.")).toBeInTheDocument();

    await user.clear(screen.getByLabelText("X (mm)"));
    await user.type(screen.getByLabelText("X (mm)"), "-1");
    await user.click(screen.getByRole("button", { name: "Go" }));

    // -1 is outside this fixture's (nonnegative) working volume, so it's
    // rejected by the generic range check rather than a sign check — there
    // is no standalone "must be 0 or greater" rule anymore (home_origin
    // configs have an entirely negative working volume).
    expect(await screen.findByText(/Move target outside working volume/)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("accepts a manual move to negative coordinates inside a home_origin (negative) working volume", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async () => jsonResponse({ status: "ok" }));
    vi.stubGlobal("fetch", fetchMock);

    render(
      <GantryPositionWidget
        position={position()}
        workingVolume={negativeWorkingVolume}
        gantryFile="cubos.yaml"
        gantry={null}
        onSaveCalibrated={async () => undefined}
      />,
    );

    await user.type(screen.getByLabelText("X (mm)"), "-200");
    await user.type(screen.getByLabelText("Y (mm)"), "-150");
    await user.type(screen.getByLabelText("Z (mm)"), "-50");
    await user.click(screen.getByRole("button", { name: "Go" }));

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/gantry/move-to",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ x: -200, y: -150, z: -50 }),
      }),
    );
    expect(screen.queryByText(/Move target outside working volume/)).not.toBeInTheDocument();
  });

  it("rejects a positive move target outside a home_origin (negative) working volume", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    render(
      <GantryPositionWidget
        position={position()}
        workingVolume={negativeWorkingVolume}
        gantryFile="cubos.yaml"
        gantry={null}
        onSaveCalibrated={async () => undefined}
      />,
    );

    await user.type(screen.getByLabelText("X (mm)"), "10");
    await user.type(screen.getByLabelText("Y (mm)"), "-150");
    await user.type(screen.getByLabelText("Z (mm)"), "-50");
    await user.click(screen.getByRole("button", { name: "Go" }));

    expect(await screen.findByText(/Move target outside working volume/)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("allows a jog that stays inside a negative (home_origin) working volume", () => {
    const fetchMock = vi.fn(async () => jsonResponse({ status: "ok" }));
    vi.stubGlobal("fetch", fetchMock);

    render(
      <GantryPositionWidget
        position={position({ x: -5, y: -5, z: -5, work_x: -5, work_y: -5, work_z: -5 })}
        workingVolume={negativeWorkingVolume}
        gantryFile="cubos.yaml"
        gantry={null}
        onSaveCalibrated={async () => undefined}
      />,
    );

    fireEvent.mouseDown(screen.getByTitle("X-"));
    fireEvent.mouseUp(screen.getByTitle("X-"));

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/gantry/jog",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ x: -0.5, y: 0, z: 0 }) }),
    );
  });

  it("rejects a jog that would push past a negative (home_origin) working-volume bound", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    render(
      <GantryPositionWidget
        // x_min=-400; already within 0.5mm of it, so an X- jog would cross the bound.
        position={position({ x: -399.7, y: -5, z: -5, work_x: -399.7, work_y: -5, work_z: -5 })}
        workingVolume={negativeWorkingVolume}
        gantryFile="cubos.yaml"
        gantry={null}
        onSaveCalibrated={async () => undefined}
      />,
    );

    fireEvent.mouseDown(screen.getByTitle("X-"));
    fireEvent.mouseUp(screen.getByTitle("X-"));

    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.getByText("At working-volume limit")).toBeInTheDocument();
  });

  it("stops held jog requests once the predicted target reaches the working volume edge", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ status: "ok" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(
      <GantryPositionWidget
        position={position({ z: 79.4, work_z: 79.4 })}
        workingVolume={workingVolume}
        gantryFile="cubos.yaml"
        gantry={null}
        onSaveCalibrated={async () => undefined}
      />,
    );

    fireEvent.mouseDown(screen.getByTitle("Z+"));
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(450);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    fireEvent.mouseUp(screen.getByTitle("Z+"));
  });

  it("does not keyboard-jog while a select has focus", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    render(
      <>
        <label>
          Mode
          <select>
            <option>Manual</option>
          </select>
        </label>
        <GantryPositionWidget
          position={position()}
          workingVolume={workingVolume}
          gantryFile="cubos.yaml"
          gantry={null}
          onSaveCalibrated={async () => undefined}
        />
      </>,
    );

    const select = screen.getByLabelText("Mode");
    select.focus();
    fireEvent.keyDown(select, { key: "ArrowDown" });

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("maps keyboard jog keys to CubOS-relative deltas", () => {
    const fetchMock = vi.fn<(input: string | URL | Request, init?: RequestInit) => Promise<Response>>(
      async () => jsonResponse({ status: "ok" }),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(
      <GantryPositionWidget
        position={position({ x: 50, y: 50, z: 50, work_x: 50, work_y: 50, work_z: 50 })}
        workingVolume={workingVolume}
        gantryFile="cubos.yaml"
        gantry={null}
        onSaveCalibrated={async () => undefined}
      />,
    );

    for (const key of ["ArrowLeft", "ArrowUp", "ArrowDown", "x", "z"]) {
      fireEvent.keyDown(window, { key });
      fireEvent.keyUp(window, { key });
    }

    const jogBodies = fetchMock.mock.calls
      .filter(([input]) => requestPath(input) === "/api/v1/gantry/jog")
      .map(([, init]) => JSON.parse(String(init?.body)));
    expect(jogBodies).toEqual([
      { x: -0.5, y: 0, z: 0 },
      { x: 0, y: 0.5, z: 0 },
      { x: 0, y: -0.5, z: 0 },
      { x: 0, y: 0, z: 0.5 },
      { x: 0, y: 0, z: -0.5 },
    ]);
  });

  it("uses typed jog distances", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn<(input: string | URL | Request, init?: RequestInit) => Promise<Response>>(
      async () => jsonResponse({ status: "ok" }),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(
      <GantryPositionWidget
        position={position({ x: 20, y: 20, z: 20, work_x: 20, work_y: 20, work_z: 20 })}
        workingVolume={workingVolume}
        gantryFile="cubos.yaml"
        gantry={null}
        onSaveCalibrated={async () => undefined}
      />,
    );

    await user.clear(screen.getByLabelText("XY mm"));
    await user.type(screen.getByLabelText("XY mm"), "1");
    fireEvent.mouseDown(screen.getByTitle("X+"));
    fireEvent.mouseUp(screen.getByTitle("X+"));

    await user.clear(screen.getByLabelText("Z mm"));
    await user.type(screen.getByLabelText("Z mm"), "10");
    fireEvent.mouseDown(screen.getByTitle("Z+"));
    fireEvent.mouseUp(screen.getByTitle("Z+"));

    const jogBodies = fetchMock.mock.calls
      .filter(([input]) => requestPath(input) === "/api/v1/gantry/jog")
      .map(([, init]) => JSON.parse(String(init?.body)));
    expect(jogBodies).toEqual([
      { x: 1, y: 0, z: 0 },
      { x: 0, y: 0, z: 10 },
    ]);
  });

  it("falls back to backend guarding when current work position is not finite", () => {
    const fetchMock = vi.fn(async () => jsonResponse({ status: "ok" }));
    vi.stubGlobal("fetch", fetchMock);

    render(
      <GantryPositionWidget
        position={position({ work_x: Number.NaN })}
        workingVolume={workingVolume}
        gantryFile="cubos.yaml"
        gantry={null}
        onSaveCalibrated={async () => undefined}
      />,
    );

    fireEvent.mouseDown(screen.getByTitle("X+"));
    fireEvent.mouseUp(screen.getByTitle("X+"));

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/gantry/jog",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ x: 0.5, y: 0, z: 0 }),
      }),
    );
  });

  it("stops keyboard hold jogging on window blur and sends jog cancel only for a held jog", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(async (input: string | URL | Request) => {
      if (requestPath(input) === "/api/v1/gantry/jog-cancel") {
        return jsonResponse(position());
      }
      return jsonResponse({ status: "ok" });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <GantryPositionWidget
        position={position()}
        workingVolume={workingVolume}
        gantryFile="cubos.yaml"
        gantry={null}
        onSaveCalibrated={async () => undefined}
      />,
    );

    fireEvent.keyDown(window, { key: "ArrowRight" });
    await vi.advanceTimersByTimeAsync(350);

    const jogCallsBeforeBlur = fetchMock.mock.calls.filter(([input]) => requestPath(input) === "/api/v1/gantry/jog").length;
    expect(jogCallsBeforeBlur).toBeGreaterThan(1);

    fireEvent.blur(window);

    const jogCallsAfterBlur = fetchMock.mock.calls.filter(([input]) => requestPath(input) === "/api/v1/gantry/jog").length;
    await vi.advanceTimersByTimeAsync(450);
    expect(fetchMock.mock.calls.filter(([input]) => requestPath(input) === "/api/v1/gantry/jog")).toHaveLength(jogCallsAfterBlur);
    expect(fetchMock.mock.calls.some(([input]) => requestPath(input) === "/api/v1/gantry/jog-cancel")).toBe(true);
  });

  it("locks manual controls and keyboard jogging while a protocol is running", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    render(
      <GantryPositionWidget
        position={position()}
        workingVolume={workingVolume}
        gantryFile="cubos.yaml"
        gantry={null}
        isRunning
        onSaveCalibrated={async () => undefined}
      />,
    );

    expect(screen.getByText("Protocol running — manual control locked")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Home" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Go" })).toBeDisabled();

    fireEvent.keyDown(window, { key: "ArrowRight" });

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does not keyboard-jog while the calibration wizard is open", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    render(
      <GantryPositionWidget
        position={position()}
        workingVolume={workingVolume}
        gantryFile="cubos.yaml"
        gantry={{ filename: "cubos.yaml", config: gantryConfig() }}
        onSaveCalibrated={async () => undefined}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Calibrate" }));
    await user.click(screen.getByRole("button", { name: "Full calibration" }));
    expect(screen.getByRole("dialog", { name: "Gantry calibration" })).toBeInTheDocument();

    fireEvent.keyDown(window, { key: "ArrowRight" });

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("opens the chooser without hardware calls and blocks background keyboard jogging", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    render(<GantryPositionWidget position={position()} workingVolume={workingVolume}
      gantryFile="cubos.yaml" gantry={{ filename: "cubos.yaml", config: gantryConfig() }}
      onSaveCalibrated={async () => undefined} onSaveInstrumentOffsets={async () => undefined} />);
    await user.click(screen.getByRole("button", { name: "Calibrate" }));
    expect(screen.getByRole("dialog", { name: "Choose calibration mode" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "Gantry calibration" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Calibrate instrument offsets" })).not.toBeInTheDocument();
    fireEvent.keyDown(window, { key: "ArrowRight" });
    expect(fetchMock).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("button", { name: "Calibrate" })).toHaveFocus();
  });

  it.each([
    [{ connected: false }, "Connect the gantry before calibrating offsets."],
    [{ calibration_active: true }, "Finish or restore full calibration before calibrating offsets."],
    [{ status: "Alarm" }, "Wait for the gantry to be idle."],
  ])("keeps full calibration available but blocks offsets for unavailable gantry state %j", async (state, reason) => {
    const user = userEvent.setup();
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    render(<GantryPositionWidget position={position(state)} workingVolume={workingVolume}
      gantryFile="cubos.yaml" gantry={{ filename: "cubos.yaml", config: gantryConfig() }}
      onSaveCalibrated={async () => undefined} onSaveInstrumentOffsets={async () => undefined} />);
    await user.click(screen.getByRole("button", { name: "Calibrate" }));
    expect(screen.getByRole("button", { name: "Full calibration" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Calibrate offsets only" })).toBeDisabled();
    expect(screen.getByText(reason)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
    if ("connected" in state && state.connected === false) {
      await user.click(screen.getByRole("button", { name: "Full calibration" }));
      expect(screen.getByRole("dialog", { name: "Gantry calibration" })).toBeInTheDocument();
      expect(screen.queryByRole("dialog", { name: "Choose calibration mode" })).not.toBeInTheDocument();
      expect(fetchMock).not.toHaveBeenCalled();
    }
  });

  it("opens offsets-only calibration without controller changes or background keyboard jogs", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    render(<GantryPositionWidget position={position()} workingVolume={workingVolume}
      gantryFile="cubos.yaml" gantry={{ filename: "cubos.yaml", config: gantryConfig() }}
      onSaveCalibrated={async () => undefined} onSaveInstrumentOffsets={async () => undefined} />);
    await user.click(screen.getByRole("button", { name: "Calibrate" }));
    await user.click(screen.getByRole("button", { name: "Calibrate offsets only" }));
    expect(screen.queryByRole("dialog", { name: "Choose calibration mode" })).not.toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Instrument offset calibration" })).toBeInTheDocument();
    fireEvent.keyDown(window, { key: "ArrowRight" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("renders jog command errors inline", async () => {
    const fetchMock = vi.fn(async (input: string | URL | Request) => {
      if (requestPath(input) === "/api/v1/gantry/jog") {
        return jsonResponse({ detail: "Target outside working volume" }, 400);
      }
      return jsonResponse({ status: "ok" });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <GantryPositionWidget
        position={position()}
        workingVolume={workingVolume}
        gantryFile="cubos.yaml"
        gantry={null}
        onSaveCalibrated={async () => undefined}
      />,
    );

    fireEvent.mouseDown(screen.getByTitle("Z+"));

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Target outside working volume"));
  });

  it("shows interrupted calibration state and restores soft limits", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async (input: string | URL | Request) => {
      if (requestPath(input) === "/api/v1/gantry/calibration/restore-soft-limits") {
        return jsonResponse(position({ calibration_active: false }));
      }
      return jsonResponse({ status: "ok" });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <GantryPositionWidget
        position={position({ calibration_active: true })}
        workingVolume={workingVolume}
        gantryFile="cubos.yaml"
        gantry={{ filename: "cubos.yaml", config: gantryConfig() }}
        onSaveCalibrated={async () => undefined}
      />,
    );

    expect(screen.getByText("Calibration interrupted — soft limits are disabled")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Restore soft limits" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/gantry/calibration/restore-soft-limits",
      expect.objectContaining({ method: "POST" }),
    ));
  });

  // Regression: Number("") is 0, so blank Move To fields used to pass the
  // finite-number check and command a silent move to 0 on every blank axis
  // (e.g. a Z plunge to deck level with only X filled in).
  it("rejects a move when all coordinate fields are blank", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    render(
      <GantryPositionWidget
        position={position()}
        workingVolume={workingVolume}
        gantryFile="cubos.yaml"
        gantry={null}
        onSaveCalibrated={async () => undefined}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Go" }));

    expect(await screen.findByText("Enter valid X, Y, and Z coordinates.")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects a move when one axis is left blank instead of treating it as 0", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    render(
      <GantryPositionWidget
        position={position()}
        workingVolume={workingVolume}
        gantryFile="cubos.yaml"
        gantry={null}
        onSaveCalibrated={async () => undefined}
      />,
    );

    await user.type(screen.getByLabelText("X (mm)"), "100");
    await user.type(screen.getByLabelText("Y (mm)"), "100");
    await user.click(screen.getByRole("button", { name: "Go" }));

    expect(await screen.findByText("Enter valid X, Y, and Z coordinates.")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("still sends an explicit all-zero move", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async () => jsonResponse({ status: "ok" }));
    vi.stubGlobal("fetch", fetchMock);

    render(
      <GantryPositionWidget
        position={position()}
        workingVolume={workingVolume}
        gantryFile="cubos.yaml"
        gantry={null}
        onSaveCalibrated={async () => undefined}
      />,
    );

    await user.type(screen.getByLabelText("X (mm)"), "0");
    await user.type(screen.getByLabelText("Y (mm)"), "0");
    await user.type(screen.getByLabelText("Z (mm)"), "0");
    await user.click(screen.getByRole("button", { name: "Go" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/gantry/move-to",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ x: 0, y: 0, z: 0 }),
      }),
    ));
  });
});
