import assert from 'node:assert/strict'
import test from 'node:test'
import {rackPhysicalBounds,rackSceneMatrix,reverseTriangleWinding,type DeckPoint} from '../src/rackGeometry.ts'

function deckPoint(matrix:ReturnType<typeof rackSceneMatrix>,native:DeckPoint){
 const sceneX=matrix[0]*native.x+matrix[1]*native.y+matrix[2]*native.z+matrix[3]
 const sceneY=matrix[4]*native.x+matrix[5]*native.y+matrix[6]*native.z+matrix[7]
 const sceneZ=matrix[8]*native.x+matrix[9]*native.y+matrix[10]*native.z+matrix[11]
 return {x:sceneX+145,y:90-sceneZ,z:sceneY-60}
}

function close(actual:DeckPoint,expected:DeckPoint){
 for(const axis of ['x','y','z'] as const)assert.ok(Math.abs(actual[axis]-expected[axis])<1e-9,`${axis}: ${actual[axis]} != ${expected[axis]}`)
}

test('registers the rack CAD from calibrated A1, A2, and B1',()=>{
 const a1={x:146.456,y:75.035,z:98.5},a2={x:156.456,y:75.035,z:98.5},b1={x:146.456,y:65.035,z:98.5}
 const matrix=rackSceneMatrix(a1,a2,b1)
 const determinant=matrix[0]*(matrix[5]*matrix[10]-matrix[6]*matrix[9])-matrix[1]*(matrix[4]*matrix[10]-matrix[6]*matrix[8])+matrix[2]*(matrix[4]*matrix[9]-matrix[5]*matrix[8])
 assert.equal(determinant,-1)
 close(deckPoint(matrix,{x:6,y:7,z:-117}),a1)
 close(deckPoint(matrix,{x:6,y:7,z:-107}),a2)
 close(deckPoint(matrix,{x:16,y:7,z:-117}),b1)
 const bounds=rackPhysicalBounds(a1,a2,b1)
 close(bounds.minimum,{x:143.456,y:-2.965,z:28.5})
 close(bounds.maximum,{x:263.456,y:81.035,z:91.5})
 assert.equal(bounds.minimum.x-139.456,4)
})

test('follows a rotated calibration basis',()=>{
 const a1={x:100,y:200,z:50},a2={x:100,y:210,z:50},b1={x:90,y:200,z:50}
 const matrix=rackSceneMatrix(a1,a2,b1)
 close(deckPoint(matrix,{x:6,y:7,z:-117}),a1)
 close(deckPoint(matrix,{x:6,y:7,z:-107}),a2)
 close(deckPoint(matrix,{x:16,y:7,z:-117}),b1)
})

test('repairs triangle winding after the calibrated reflection',()=>{
 const positions=new Float32Array([0,0,0,1,0,0,0,1,0])
 reverseTriangleWinding(positions)
 assert.deepEqual(Array.from(positions),[0,0,0,0,1,0,1,0,0])
})
