export type Point={id:string;x:number;y:number;z:number}
export type Event={kind:string;t:number;duration:number;step:number;command:string;start?:number[];end?:number[];gcode?:string;target?:string;volume?:number;total?:number;color?:string;attached?:boolean;extension?:number}
export type Trial={trial:number;well:string;volumes:number[];color:string;loss:number;reason:string}
export type Mount={offset_x:number;offset_y:number;depth:number}
export type RouteSegment={index:number;step?:number;phase?:string;start:number[];end:number[];axes?:string;duration?:number;command?:string;reason?:string}
export type PlanStep={index:number;command:string;args:Record<string,unknown>;summary:string}
export type Route={schema_version?:string;source?:string;planner_enabled?:boolean;strategy?:string;strategies?:string[];axis_order?:string[];reason?:string;segments:RouteSegment[];execution_segments?:RouteSegment[];fixtures?:{name:string;minimum:{x:number;y:number;z:number};maximum:{x:number;y:number;z:number}}[];parity?:{segments_equal:boolean;checked:boolean}}
export type Run={events:Event[];duration:number;steps:number;points:Record<string,Point[]>;fluids:Record<string,{volume:number;color:string;parts:number[]}>;deck_metadata:Record<string,{height?:number;length?:number;width?:number;vial_height?:number;vial_diameter?:number;diameter?:number;capacity_ul?:number;working_volume_ul?:number;tip_length?:number;side_exit?:{lift_mm:number;exit_x:number};location?:{x:number;y:number;z:number}}>;mounts:Record<string,Mount>;initial_position:number[];requested_trials?:number;stop_reason?:string|null;history?:Trial[];protocol_yaml?:string;plan?:PlanStep[];route?:Route;routing?:Record<string,unknown>}
export type Bundle={id?:string;name?:string;description?:string;mode?:string;gantry_yaml:string;deck_yaml:string;protocol_yaml:string;stocks:{name:string;target:string;hex:string}[];assumptions:string[];routing?:Record<string,any>;initial_position?:number[]|null}
export type Policy={target:string;trials:number;initial:number;total:number;acquisition:string;exploration:number;seed:number}
export function stateAt(run:Run|null,time:number){
 const pose=run?.initial_position.slice()??[230,90,65]; let tip=0; let current:Event|undefined;const wells:Record<string,{color:string;volume:number}>={};let capture:Event|undefined;const used=new Set<string>();
 for(const e of run?.events??[]){if(e.t>time)break;current=e;
 if(e.kind==='move'&&e.start&&e.end){const f=Math.min(1,(time-e.t)/Math.max(.001,e.duration));for(let i=0;i<3;i++)pose[i]=e.start[i]+(e.end[i]-e.start[i])*f}
 if(e.kind==='tip'){tip=e.extension??0;if(e.attached&&e.target)used.add(e.target)}
 if(e.kind==='dispense'&&e.target&&e.color)wells[e.target]={color:e.color,volume:e.total??0};
 if(e.kind==='capture')capture=e;
 }
 return {pose,tip,current,wells,capture,used};
}
