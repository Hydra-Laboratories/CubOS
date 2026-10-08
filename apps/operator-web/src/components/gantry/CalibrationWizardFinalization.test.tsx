import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import CalibrationWizard from "./CalibrationWizard";
import type { FinalizeOriginResponse, GantryConfig, GantryPosition } from "../../types";
import { validateFinalizedOrigin } from "./calibrationMath";

vi.mock("./CameraPreview", () => ({ default: () => <div>Camera preview</div> }));
const position: GantryPosition = {x:50,y:40,z:0,work_x:50,work_y:40,work_z:0,status:"Idle",connected:true,calibration_active:false};
function fixture(policy: "deck_origin" | "home_origin", multi: boolean) {
  const config: GantryConfig = {
    serial_port:"/dev/offline",gantry_type:"cub_xl",origin_policy:policy,
    cnc:{factory_z_travel_mm:80,calibration_block_height_mm:35,safe_z:policy === "home_origin" ? -5 : 76},
    working_volume:{x_min:0,x_max:258,y_min:0,y_max:145,z_min:0,z_max:80},
    instruments:{pipette:{type:"pipette",vendor:"sartorius",offset_x:0,offset_y:0,depth:0},
      ...(multi ? {camera:{type:"camera",vendor:"usb",offset_x:0,offset_y:0,depth:0}} : {})},
  };
  const result: FinalizeOriginResponse = {
    origin_policy:policy,
    safe_z:policy === "home_origin" ? -5 : 76,
    working_volume:policy === "home_origin"
      ? {x_min:-258,x_max:0,y_min:-145,y_max:0,z_min:-80,z_max:0}
      : {x_min:0,x_max:258,y_min:0,y_max:145,z_min:1,z_max:81},
    measured_volume:{x:258,y:145,z:81},max_travel:{x:259,y:146,z:81},
    position:policy === "home_origin" ? {x:0,y:0,z:0} : {x:258,y:145,z:81},
    z_calibration:{block_height:35,total_z_range:80,home_z:0,block_touch_z:-46,home_to_block_travel:46,
      remaining_below_block:34,can_reach_deck_bottom:false,z_min:1,z_max:81,max_travel_z:80},homing_pull_off_mm:1,
  };
  return {config,result};
}
async function setup(policy: "deck_origin" | "home_origin", multi: boolean, change?: (result: FinalizeOriginResponse) => void, selectedPolicy = policy) {
  const user = userEvent.setup();
  const {config} = fixture(policy,multi);
  const {result} = fixture(selectedPolicy,multi);
  change?.(result);
  const save = vi.fn<(filename: string, config: GantryConfig) => Promise<void>>(async () => undefined);
  let reads = 0;
  const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL(typeof input === "string" ? input : input instanceof URL ? input : input.url,"http://localhost").pathname;
    let body: unknown = position;
    if (path.endsWith("/position")) {
      reads++;
      body = {...position,x:reads === 1 ? 50 : 60,y:reads === 1 ? 40 : 45,
        work_x:reads === 1 ? 50 : 60,work_y:reads === 1 ? 40 : 45,
        z:reads === 1 ? -46 : 55,work_z:reads === 1 ? -46 : 55};
    }
    if (path.endsWith("/home-and-center")) body = {xy_bounds:{x:258,y:145,z:0},position:{x:50,y:40,z:0}};
    if (path.endsWith("/work-coordinates")) body = {...position,z:35,work_z:35};
    if (path.endsWith("/finalize-origin")) {
      expect(JSON.parse(String(init?.body)).origin_policy).toBe(selectedPolicy);
      body = result;
    }
    return new Response(JSON.stringify(body),{status:200,headers:{"Content-Type":"application/json"}});
  });
  vi.stubGlobal("fetch",fetch);
  render(<CalibrationWizard open gantry={{filename:"hein.yaml",config}} position={position} onClose={() => undefined} onSaveCalibrated={save} />);
  expect(screen.getByLabelText("Coordinate origin")).toHaveValue(policy);
  await user.selectOptions(screen.getByLabelText("Coordinate origin"),selectedPolicy);
  await user.click(screen.getByRole("button",{name:"Continue"}));
  await user.click(screen.getByRole("button",{name:"Home gantry"}));
  if (multi) await user.click(await screen.findByRole("button",{name:"Set XY origin and continue"}));
  await user.click(await screen.findByRole("button",{name:"Continue"}));
  await user.click(screen.getByLabelText("Calibrating with a tip attached"));
  await user.type(screen.getByLabelText("Tip length (mm)"),"40");
  await user.click(screen.getByRole("button",{name:multi ? "Set Z reference with pipette and continue" : "Set origin and continue"}));
  if (multi) {
    await user.type(await screen.findByLabelText("Distance from calibration block (mm)"),"20");
    await user.click(screen.getByRole("button",{name:"Record camera"}));
  }
  await screen.findByRole("button",{name:"Save"});
  return {user,save,fetch,result};
}
afterEach(() => vi.unstubAllGlobals());
describe("verified frame finalization",() => {
  it.each([["deck_origin",false],["home_origin",false],["deck_origin",true],["home_origin",true]] as const)("saves verified %s bounds for multi=%s",async (policy,multi) => {
    const {user,save,fetch,result} = await setup(policy,multi);
    await user.click(screen.getByRole("button",{name:"Save"}));
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    const saved = save.mock.calls[0][1] as GantryConfig;
    expect(saved.working_volume).toEqual(result.working_volume);
    expect(saved.cnc.safe_z).toBe(result.safe_z);
    expect(saved.instruments.pipette.depth).toBe(-40);
    if (multi) expect(saved.instruments.camera).toMatchObject({offset_x:-10,offset_y:-5,depth:0});
    const calls = fetch.mock.calls.map(([input]) => String(input));
    expect(calls.filter(path => path.endsWith("/finalize-origin"))).toHaveLength(1);
    expect(calls.some(path => path.endsWith("/home") || path.endsWith("/soft-limits"))).toBe(false);
  });
  it.each([["deck_origin","home_origin"],["home_origin","deck_origin"]] as const)("uses explicit %s to %s choice and verified safe height",async (initial,selected) => {
    const {user,save,result} = await setup(initial,true,undefined,selected);
    await user.click(screen.getByRole("button",{name:"Save"}));
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    const saved = save.mock.calls[0][1];
    expect(saved.origin_policy).toBe(selected);
    expect(saved.working_volume).toEqual(result.working_volume);
    expect(saved.cnc.safe_z).toBe(selected === "home_origin" ? -5 : 76);
  });
  it.each(["wrong policy","missing frame","wrong readback","out of bounds within tolerance"])("does not save %s",async mode => {
    const {user,save} = await setup("home_origin",true,result => {
      if (mode === "wrong policy") result.origin_policy = "deck_origin";
      if (mode === "missing frame") delete (result as Partial<FinalizeOriginResponse>).working_volume;
      if (mode === "wrong readback") result.position = {x:258,y:145,z:81};
      if (mode === "out of bounds within tolerance") result.position.x = 0.1;
    });
    await user.click(screen.getByRole("button",{name:"Save"}));
    await screen.findByText(/controller|bounds and controller position/i);
    expect(save).not.toHaveBeenCalled();
  });
  it("retries a failed file save without repeating frame finalization",async () => {
    const {user,save,fetch} = await setup("home_origin",true);
    save.mockRejectedValueOnce(new Error("Disk full"));
    await user.click(screen.getByRole("button",{name:"Save"}));
    await screen.findByText(/Disk full/);
    await user.click(screen.getByRole("button",{name:"Save"}));
    await waitFor(() => expect(save).toHaveBeenCalledTimes(2));
    expect(fetch.mock.calls.filter(([input]) => String(input).endsWith("/finalize-origin"))).toHaveLength(1);
    expect(save.mock.calls[0][1]).toEqual(save.mock.calls[1][1]);
  });
  it.each(["missing position","nonfinite position","wrong bounds","nonfinite limits","missing safe height","wrong safe height"])("rejects %s",mode => {
    const {config,result} = fixture("home_origin",true);
    if (mode === "missing position") delete (result as Partial<FinalizeOriginResponse>).position;
    if (mode === "nonfinite position") result.position.z = NaN;
    if (mode === "wrong bounds") result.working_volume.z_min = -81;
    if (mode === "nonfinite limits") result.max_travel.z = Infinity;
    if (mode === "missing safe height") delete (result as Partial<FinalizeOriginResponse>).safe_z;
    if (mode === "wrong safe height") result.safe_z = 76;
    expect(() => validateFinalizedOrigin(config,result)).toThrow();
  });
});
