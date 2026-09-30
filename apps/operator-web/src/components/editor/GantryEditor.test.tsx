import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import GantryEditor from "./GantryEditor";
import type { GantryResponse, InstrumentSchemas, InstrumentTypeInfo } from "../../types";

const INSTRUMENT_TYPES: InstrumentTypeInfo[] = [
  { type: "pipette", vendors: ["opentrons"], is_mock: false },
  { type: "asmi", vendors: ["vernier", "customer"], is_mock: false },
];

const INSTRUMENT_SCHEMAS: InstrumentSchemas = {
  pipette: {
    opentrons: [{ name: "port", type: "str", required: true, default: "/dev/ttyUSB0", choices: null }],
  },
  asmi: {
    vernier: [
      { name: "mode", type: "str", required: false, default: "a", choices: ["a", "b"] },
      { name: "enabled", type: "bool", required: false, default: false, choices: null },
      { name: "gain", type: "float", required: true, default: 1, choices: null },
      { name: "label", type: "str", required: false, default: "x", choices: null },
    ],
    customer: [
      { name: "dll_path", type: "str", required: true, default: "/opt/asmi.dll", choices: null },
    ],
  },
};

function gantryFixture(overrides: Partial<GantryResponse["config"]> = {}): GantryResponse {
  return {
    filename: "cubos.yaml",
    config: {
      serial_port: "/dev/ttyUSB0",
      gantry_type: "cub_xl",
      cnc: {
      factory_z_travel_mm: 80,
        calibration_block_height_mm: 35,
        y_axis_motion: "head",
        safe_z: 80,
      },
      working_volume: { x_min: 0, x_max: 300, y_min: 0, y_max: 200, z_min: 0, z_max: 80 },
      grbl_settings: { soft_limits: true, steps_per_mm_x: 400 },
      instruments: {
        pipette_1: {
          type: "pipette",
          vendor: "opentrons",
          offset_x: 1,
          offset_y: 2,
          depth: 0,
          port: "/dev/ttyUSB0",
        },
        asmi_1: {
          type: "asmi",
          vendor: "vernier",
          offset_x: 0,
          offset_y: 0,
          depth: 0,
          mode: "a",
          enabled: false,
          gain: 1,
          label: "x",
        },
      },
      ...overrides,
    },
  };
}

function renderGantry(overrides: Partial<React.ComponentProps<typeof GantryEditor>> = {}) {
  const gantry = overrides.gantry ?? gantryFixture();
  const props = {
    configs: ["cubos.yaml"],
    selectedFile: "cubos.yaml" as string | null,
    onSelectFile: vi.fn(),
    onImportFile: vi.fn(),
    gantry,
    baseline: gantry,
    instrumentTypes: INSTRUMENT_TYPES,
    instrumentSchemas: INSTRUMENT_SCHEMAS,
    onSave: vi.fn(),
    onLocalChange: vi.fn(),
    onRefresh: vi.fn(),
    ...overrides,
  };
  render(<GantryEditor {...props} />);
  return props;
}

function renderStatefulGantry(initial: GantryResponse = gantryFixture()) {
  const onLocalChange = vi.fn();
  const onSave = vi.fn();
  const onSelectFile = vi.fn();
  const onRefresh = vi.fn();

  function Harness() {
    const [gantry, setGantry] = useState(initial);
    return (
      <GantryEditor
        configs={["cubos.yaml"]}
        selectedFile="cubos.yaml"
        onSelectFile={onSelectFile}
        onImportFile={vi.fn()}
        gantry={gantry}
        baseline={initial}
        instrumentTypes={INSTRUMENT_TYPES}
        instrumentSchemas={INSTRUMENT_SCHEMAS}
        onSave={onSave}
        onLocalChange={(next) => {
          onLocalChange(next);
          setGantry(next);
        }}
        onRefresh={onRefresh}
      />
    );
  }

  render(<Harness />);
  return { onLocalChange, onSave, onSelectFile, onRefresh };
}

describe("GantryEditor", () => {
  it("renders connection, instruments, and all instrument-schema field types", () => {
    renderGantry();
    expect(screen.getByLabelText("Serial port")).toHaveValue("/dev/ttyUSB0");
    expect(screen.getByLabelText("Port *")).toHaveValue("/dev/ttyUSB0");
    // asmi schema fields: choices, bool, float, str
    expect(screen.getByLabelText("Mode")).toBeInTheDocument();
    expect(screen.getByLabelText("Enabled")).toBeInTheDocument();
    expect(screen.getByLabelText("Gain *")).toHaveValue("1");
    expect(screen.getByLabelText("Label")).toHaveValue("x");
  });

  it("renders section headings in order: Gantry, Connection, Instruments, Working Volume", () => {
    renderGantry();
    const headings = screen.getAllByRole("heading", { level: 4 });
    const sectionNames = headings
      .map((h) => h.textContent)
      .filter((text): text is string =>
        text === "Gantry" || text === "Instruments" || text === "Connection" || text === "Working Volume",
      );
    expect(sectionNames).toEqual(["Gantry", "Connection", "Instruments", "Working Volume"]);
  });

  it("shows only gantry type and serial port in the top-level Gantry section", () => {
    renderGantry();
    const heading = screen.getByRole("heading", { level: 4, name: "Gantry" });
    const card = heading.parentElement as HTMLElement;
    expect(within(card).getByLabelText(/Gantry type/)).toBeInTheDocument();
    expect(within(card).getByLabelText("Serial port")).toBeInTheDocument();
    // Nothing else leaks into the top-level card — no Connection-only
    // fields such as Factory Z travel, and no other sections.
    expect(card).not.toHaveTextContent("Factory Z travel");
    expect(card).not.toHaveTextContent("Instruments");
    expect(card).not.toHaveTextContent("Working Volume");
  });

  it("never renders a Y-axis motion control (unnecessary for operators)", async () => {
    const user = userEvent.setup();
    renderGantry();
    expect(screen.queryByLabelText(/Y-axis motion/i)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Advanced settings/ }));
    expect(screen.queryByLabelText(/Y-axis motion/i)).not.toBeInTheDocument();
  });

  describe("raw YAML mode", () => {
    it("shows the current config as YAML text and hides the structured form", async () => {
      const user = userEvent.setup();
      renderGantry();

      await user.click(screen.getByRole("button", { name: "Edit raw YAML" }));

      const textarea = screen.getByLabelText("Raw gantry YAML") as HTMLTextAreaElement;
      expect(textarea.value).toContain("serial_port: /dev/ttyUSB0");
      expect(textarea.value).toContain("gantry_type: cub_xl");
      // The structured form (including the Gantry identity card) is gone
      // while raw mode is active — one editable source of truth at a time.
      expect(screen.queryByLabelText(/Gantry type/)).not.toBeInTheDocument();
      expect(screen.queryByLabelText("Serial port")).not.toBeInTheDocument();
      expect(screen.queryByRole("heading", { name: "Connection" })).not.toBeInTheDocument();
    });

    it("edits flow from the raw textarea into the underlying config and back out on save", async () => {
      const user = userEvent.setup();
      const props = renderGantry();

      await user.click(screen.getByRole("button", { name: "Edit raw YAML" }));
      const textarea = screen.getByLabelText("Raw gantry YAML");
      const nextYaml = [
        "serial_port: /dev/ttyUSB7",
        "gantry_type: cub",
        "cnc:",
        "  factory_z_travel_mm: 80",
        "working_volume:",
        "  x_min: 0",
        "  x_max: 300",
        "  y_min: 0",
        "  y_max: 200",
        "  z_min: 0",
        "  z_max: 80",
        "instruments: {}",
        "",
      ].join("\n");
      // A raw YAML blob (with literal braces/brackets) is entered as one
      // paste rather than keystroke-by-keystroke — user.type() treats `{`
      // and `[` as special key-sequence syntax, which a real YAML document
      // will always contain.
      fireEvent.change(textarea, { target: { value: nextYaml } });

      expect(props.onLocalChange).toHaveBeenCalled();
      const lastPayload = vi.mocked(props.onLocalChange).mock.lastCall?.[0] as GantryResponse;
      expect(lastPayload.config.serial_port).toBe("/dev/ttyUSB7");
      expect(lastPayload.config.gantry_type).toBe("cub");

      await user.click(screen.getByRole("button", { name: "Save" }));
      expect(props.onSave).toHaveBeenCalledWith(
        "cubos.yaml",
        expect.objectContaining({ serial_port: "/dev/ttyUSB7", gantry_type: "cub" }),
      );
    });

    it("blocks saving and switching back to the form while the YAML doesn't parse", async () => {
      const user = userEvent.setup();
      renderGantry();

      await user.click(screen.getByRole("button", { name: "Edit raw YAML" }));
      const textarea = screen.getByLabelText("Raw gantry YAML");
      fireEvent.change(textarea, { target: { value: "serial_port: [unterminated" } });

      expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
      const backButton = screen.getByRole("button", { name: "Back to form" });
      expect(backButton).toBeDisabled();
      await user.click(backButton);
      // Still in raw mode — the click was a no-op while invalid.
      expect(screen.getByLabelText("Raw gantry YAML")).toBeInTheDocument();
    });

    it("returns to the structured form once the YAML is valid again", async () => {
      const user = userEvent.setup();
      renderGantry();

      await user.click(screen.getByRole("button", { name: "Edit raw YAML" }));
      await user.click(screen.getByRole("button", { name: "Back to form" }));

      expect(screen.queryByLabelText("Raw gantry YAML")).not.toBeInTheDocument();
      expect(screen.getByLabelText("Serial port")).toHaveValue("/dev/ttyUSB0");
    });
  });

  it("keeps GRBL under the Advanced settings expander", async () => {
    const user = userEvent.setup();
    renderGantry();

    expect(screen.queryByLabelText("Steps/mm X")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Advanced settings/ }));
    expect(screen.getByLabelText("Steps/mm X")).toHaveValue("400");
    expect(screen.getByLabelText("Soft limits")).toBeInTheDocument();
  });

  it("auto-expands Advanced settings and marks it dirty when GRBL differs from the saved baseline", () => {
    const gantry = gantryFixture({ grbl_settings: { steps_per_mm_x: 400 } });
    const baseline = gantryFixture({ grbl_settings: { steps_per_mm_x: 200 } });
    renderGantry({ gantry, baseline });

    // Expanded on mount — no click needed — because GRBL has unsaved edits.
    expect(screen.getByRole("button", { name: /Advanced settings/ })).toHaveAttribute("aria-expanded", "true");
    // Label gains an amber "*" when dirty, so match loosely.
    expect(screen.getByLabelText(/Steps\/mm X/)).toHaveValue("400");
    expect(screen.getByRole("button", { name: /Advanced settings/ })).toHaveTextContent("*");
  });

  it("clears an optional GRBL number field", async () => {
    const user = userEvent.setup();
    const props = renderGantry();
    await user.click(screen.getByRole("button", { name: /Advanced settings/ }));

    // Most GRBL number fields are unset (Clear disabled); click the one
    // that actually has a value (steps_per_mm_x).
    const enabledClear = screen
      .getAllByRole("button", { name: "Clear" })
      .find((b) => !(b as HTMLButtonElement).disabled)!;
    await user.click(enabledClear);
    expect(props.onLocalChange).toHaveBeenCalled();
  });

  it("starts a brand-new config and reports the edit", async () => {
    const user = userEvent.setup();
    const props = renderGantry({ gantry: null, baseline: null, selectedFile: null });

    expect(screen.queryByLabelText("Serial port")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "+ New config" }));
    expect(props.onLocalChange).toHaveBeenCalled();
    expect(await screen.findByLabelText("Serial port")).toBeInTheDocument();
  });

  it("directs operators to choose instruments when a gantry has none", () => {
    renderGantry({ gantry: gantryFixture({ instruments: {} }) });

    expect(screen.getByText(/No mounted instruments yet/i)).toHaveTextContent(
      /Choose the instruments installed on this machine/i,
    );
  });

  it("adds and removes an instrument", async () => {
    const user = userEvent.setup();
    const props = renderGantry();

    expect(screen.getAllByRole("button", { name: "Remove" })).toHaveLength(2);
    await user.click(screen.getByRole("button", { name: "+ Add" }));
    expect(props.onLocalChange).toHaveBeenCalled();
    expect(screen.getAllByRole("button", { name: "Remove" })).toHaveLength(3);

    await user.click(screen.getAllByRole("button", { name: "Remove" })[2]);
    expect(screen.getAllByRole("button", { name: "Remove" })).toHaveLength(2);
  });

  it("shows the unsaved banner only when dirty", () => {
    const { unmount } = render(<GantryEditor {...renderProps()} dirty={false} />);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    unmount();

    render(<GantryEditor {...renderProps()} dirty />);
    const banner = screen.getByRole("alert");
    expect(banner).toHaveTextContent(/Unsaved changes/i);
    expect(banner).toHaveTextContent(/save this gantry/i);
  });

  it("edits connection selects, working volume, GRBL booleans, and instrument schema fields", async () => {
    const user = userEvent.setup();
    const props = renderGantry();

    await user.selectOptions(screen.getByLabelText(/Gantry type/), "cub");
    await user.clear(screen.getByLabelText("X max"));
    await user.type(screen.getByLabelText("X max"), "250");

    // asmi instrument schema fields: choices, bool, float, str
    await user.selectOptions(screen.getByLabelText("Mode"), "b");
    await user.selectOptions(screen.getByLabelText("Enabled"), "true");
    await user.clear(screen.getByLabelText("Gain *"));
    await user.type(screen.getByLabelText("Gain *"), "2");

    // GRBL boolean inside the Advanced panel
    await user.click(screen.getByRole("button", { name: /Advanced settings/ }));
    await user.selectOptions(screen.getByLabelText("Soft limits"), "false");

    expect(props.onLocalChange).toHaveBeenCalled();
  });

  it("shows each instrument's hardware mode and badges simulated (offline) instruments", async () => {
    const user = userEvent.setup();
    const gantry = gantryFixture();
    gantry.config.instruments.pipette_1.offline = true;
    const props = renderGantry({ gantry, baseline: gantry });

    const pipetteMode = screen.getByLabelText("Hardware", { selector: "#pipette_1-offline" });
    const asmiMode = screen.getByLabelText("Hardware", { selector: "#asmi_1-offline" });
    expect(pipetteMode).toHaveValue("true");
    expect(asmiMode).toHaveValue("false");
    expect(screen.getAllByText("simulated")).toHaveLength(1);

    await user.selectOptions(pipetteMode, "false");
    expect(props.onLocalChange).toHaveBeenCalled();
    expect(screen.queryByText("simulated")).not.toBeInTheDocument();
    const saved = vi.mocked(props.onLocalChange).mock.calls.at(-1)?.[0] as GantryResponse;
    expect(saved.config.instruments.pipette_1.offline).toBe(false);
  });

  it("switches instrument fields when the vendor changes", async () => {
    const user = userEvent.setup();
    renderStatefulGantry();

    expect(screen.getByLabelText("Mode")).toBeInTheDocument();
    const vendorSelect = screen
      .getAllByRole("combobox", { name: /Vendor/ })
      .find((select) => (select as HTMLSelectElement).value === "vernier");
    expect(vendorSelect).toBeDefined();
    await user.selectOptions(vendorSelect!, "customer");

    expect(screen.queryByLabelText("Mode")).not.toBeInTheDocument();
    expect(await screen.findByLabelText(/Dll Path/)).toHaveValue("/opt/asmi.dll");
  });

  it("remaps vendor and schema defaults when an instrument type changes", async () => {
    const { onLocalChange } = renderStatefulGantry();

    const asmiType = screen
      .getAllByLabelText(/^Type/)
      .find((input) => (input as HTMLInputElement).value === "asmi");
    expect(asmiType).toBeDefined();

    fireEvent.change(asmiType!, { target: { value: "pipette" } });

    await waitFor(() => expect(onLocalChange).toHaveBeenCalled());

    const lastPayload = onLocalChange.mock.lastCall?.[0] as GantryResponse;
    expect(lastPayload.config.instruments.asmi_1).toEqual(expect.objectContaining({
      type: "pipette",
      vendor: "opentrons",
      offset_x: 0,
      offset_y: 0,
      depth: 0,
      port: "/dev/ttyUSB0",
    }));
    expect(lastPayload.config.instruments.asmi_1.vendor).not.toBe("vernier");
    // Stale ASMI-only schema fields must not leak into the pipette instrument.
    expect(lastPayload.config.instruments.asmi_1).not.toHaveProperty("mode");
    expect(lastPayload.config.instruments.asmi_1).not.toHaveProperty("enabled");
    expect(lastPayload.config.instruments.asmi_1).not.toHaveProperty("gain");
    expect(lastPayload.config.instruments.asmi_1).not.toHaveProperty("label");
  });

  it("disables Save when the working volume is invalid", async () => {
    const user = userEvent.setup();
    renderGantry();
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();

    const xMin = screen.getByLabelText("X min");
    await user.clear(xMin);
    await user.type(xMin, "400"); // x_min >= x_max (300)
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("saves a valid config", async () => {
    const user = userEvent.setup();
    const props = renderGantry();
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(props.onSave).toHaveBeenCalledWith("cubos.yaml", expect.objectContaining({ serial_port: "/dev/ttyUSB0" }));
    expect(props.onSelectFile).toHaveBeenCalledWith("cubos.yaml");
  });

  it("shows a save-failed banner and clears it on the next edit or successful save", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn()
      .mockRejectedValueOnce(new Error("400: bad gantry"))
      .mockResolvedValueOnce(undefined);
    renderGantry({ onSave });

    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/Save failed/i)).toHaveTextContent("400: bad gantry");

    // Any further edit clears the stale error, even before a retry.
    await user.type(screen.getByLabelText("Serial port"), "9");
    expect(screen.queryByText(/Save failed/i)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(2));
    expect(screen.queryByText(/Save failed/i)).not.toBeInTheDocument();
  });

  it("discards local edits back to the last-saved baseline and notifies the parent", async () => {
    const user = userEvent.setup();
    const onRefresh = vi.fn();
    renderGantry({ dirty: true, onRefresh });

    // Per-field dirty markers (an amber "*") append to the accessible
    // name once the value diverges from baseline, hence the loose match.
    const serialPort = screen.getByLabelText("Serial port");
    await user.clear(serialPort);
    await user.type(serialPort, "/dev/ttyUSB9");
    expect(screen.getByLabelText(/^Serial port/)).toHaveValue("/dev/ttyUSB9");

    await user.click(screen.getByRole("button", { name: "Discard changes" }));
    expect(await screen.findByRole("alertdialog")).toHaveTextContent("Discard unsaved gantry changes?");
    await user.click(screen.getByRole("button", { name: "Discard" }));

    expect(onRefresh).toHaveBeenCalled();
    expect(screen.getByLabelText("Serial port")).toHaveValue("/dev/ttyUSB0");
  });

  it("keeps edits when the user cancels the discard confirm", async () => {
    const user = userEvent.setup();
    const onRefresh = vi.fn();
    renderGantry({ dirty: true, onRefresh });

    const serialPort = screen.getByLabelText("Serial port");
    await user.clear(serialPort);
    await user.type(serialPort, "/dev/ttyUSB9");

    await user.click(screen.getByRole("button", { name: "Discard changes" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onRefresh).not.toHaveBeenCalled();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(screen.getByLabelText(/^Serial port/)).toHaveValue("/dev/ttyUSB9");
  });
});

function renderProps() {
  const gantry = gantryFixture();
  return {
    configs: ["cubos.yaml"],
    selectedFile: "cubos.yaml" as string | null,
    onSelectFile: vi.fn(),
    onImportFile: vi.fn(),
    gantry,
    baseline: gantry,
    instrumentTypes: INSTRUMENT_TYPES,
    instrumentSchemas: INSTRUMENT_SCHEMAS,
    onSave: vi.fn(),
    onLocalChange: vi.fn(),
    onRefresh: vi.fn(),
  };
}
