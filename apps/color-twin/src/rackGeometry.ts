export type DeckPoint = {x:number;y:number;z:number}

const NATIVE_A1 = {x:6,y:7,z:-117}
const NATIVE_BOUNDS = {minimum:{x:0,y:-63,z:-120},maximum:{x:84,y:0,z:0}}

function unit(from:DeckPoint,to:DeckPoint){
 const x=to.x-from.x,y=to.y-from.y,length=Math.hypot(x,y)
 if(length<1e-6)throw new Error('Rack calibration points must define a direction.')
 return {x:x/length,y:y/length}
}

export function rackSceneMatrix(a1:DeckPoint,a2:DeckPoint,b1:DeckPoint){
 const column=unit(a1,a2),row=unit(a1,b1)
 return [
  row.x,0,column.x,a1.x-145-row.x*NATIVE_A1.x-column.x*NATIVE_A1.z,
  0,1,0,a1.z+60-NATIVE_A1.y,
  -row.y,0,-column.y,90-a1.y+row.y*NATIVE_A1.x+column.y*NATIVE_A1.z,
  0,0,0,1,
 ] as const
}

export function rackPhysicalBounds(a1:DeckPoint,a2:DeckPoint,b1:DeckPoint){
 const matrix=rackSceneMatrix(a1,a2,b1)
 const points=[] as DeckPoint[]
 for(const x of [NATIVE_BOUNDS.minimum.x,NATIVE_BOUNDS.maximum.x]){
  for(const y of [NATIVE_BOUNDS.minimum.y,NATIVE_BOUNDS.maximum.y]){
   for(const z of [NATIVE_BOUNDS.minimum.z,NATIVE_BOUNDS.maximum.z]){
    const sceneX=matrix[0]*x+matrix[1]*y+matrix[2]*z+matrix[3]
    const sceneY=matrix[4]*x+matrix[5]*y+matrix[6]*z+matrix[7]
    const sceneZ=matrix[8]*x+matrix[9]*y+matrix[10]*z+matrix[11]
    points.push({x:sceneX+145,y:90-sceneZ,z:sceneY-60})
   }
  }
 }
 return {
  minimum:{x:Math.min(...points.map(p=>p.x)),y:Math.min(...points.map(p=>p.y)),z:Math.min(...points.map(p=>p.z))},
  maximum:{x:Math.max(...points.map(p=>p.x)),y:Math.max(...points.map(p=>p.y)),z:Math.max(...points.map(p=>p.z))},
 }
}

export function reverseTriangleWinding<T extends {length:number;[index:number]:number}>(positions:T){
 for(let triangle=0;triangle<positions.length;triangle+=9){
  for(let axis=0;axis<3;axis++){
   const value=positions[triangle+3+axis]
   positions[triangle+3+axis]=positions[triangle+6+axis]
   positions[triangle+6+axis]=value
  }
 }
 return positions
}
