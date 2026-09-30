import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { CoordinateField, NumberField, OptionalNumberField, SelectField, TextField, UnsavedNotice } from "./fields";

describe("fields", () => {
  it("NumberField ignores incomplete input and reverts on blur", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<NumberField label="Z" value={3} onChange={onChange} />);
    const input = screen.getByLabelText("Z");

    await user.clear(input);
    await user.type(input, "-"); // not a number yet → no onChange
    expect(onChange).not.toHaveBeenCalled();

    await user.type(input, "5"); // "-5"
    expect(onChange).toHaveBeenLastCalledWith(-5);

    await user.clear(input);
    await user.tab(); // blur reverts the raw text to the committed value
    expect(input).toHaveValue("3");
  });

  it("OptionalNumberField reports null (not an empty string) when cleared", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<OptionalNumberField label="Limit" value={-5} onChange={onChange} />);
    const input = screen.getByLabelText("Limit");
    expect(input).toHaveValue("-5");

    await user.clear(input);
    expect(onChange).toHaveBeenLastCalledWith(null);
    expect(input).toHaveValue("");

    await user.type(input, "-2.5");
    expect(onChange).toHaveBeenLastCalledWith(-2.5);
  });

  it("OptionalNumberField starts blank (not 0) when value is unset", () => {
    render(<OptionalNumberField label="Limit" value={null} onChange={vi.fn()} />);
    expect(screen.getByLabelText("Limit")).toHaveValue("");
  });

  it("TextField flags a blank required value", () => {
    render(<TextField label="Name" value="" onChange={vi.fn()} required />);
    const input = screen.getByLabelText(/Name/);
    expect(input).toHaveStyle({ borderColor: "#dc2626" });
  });

  it("CoordinateField edits each axis and reverts invalid axis input on blur", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<CoordinateField label="Loc" value={{ x: 1, y: 2, z: 3 }} onChange={onChange} />);

    await user.clear(screen.getByLabelText("Loc X"));
    await user.type(screen.getByLabelText("Loc X"), "9");
    expect(onChange).toHaveBeenLastCalledWith({ x: 9, y: 2, z: 3 });

    const yField = screen.getByLabelText("Loc Y");
    await user.clear(yField);
    await user.tab();
    expect(yField).toHaveValue("2");
  });

  it("SelectField renders options and reports the chosen value", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<SelectField label="Pick" value="a" options={["a", "b"]} onChange={onChange} />);
    await user.selectOptions(screen.getByLabelText("Pick"), "b");
    expect(onChange).toHaveBeenCalledWith("b");
  });

  it("SelectField shows a placeholder option when empty", () => {
    render(<SelectField label="Pick" value="" options={[]} onChange={vi.fn()} />);
    expect(screen.getByText("No configs found")).toBeInTheDocument();
  });

  it("UnsavedNotice renders as an alert", () => {
    render(<UnsavedNotice>heads up</UnsavedNotice>);
    expect(screen.getByRole("alert")).toHaveTextContent("heads up");
  });
});
