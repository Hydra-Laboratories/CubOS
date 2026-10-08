import {useEffect,useRef} from 'react'
import * as THREE from 'three'
import {STLLoader} from 'three/addons/loaders/STLLoader.js'
import {OrbitControls} from 'three/addons/controls/OrbitControls.js'
import {rackSceneMatrix,reverseTriangleWinding} from './rackGeometry'
import {stateAt,type Run} from './types'

type Bounds={minimum:{x:number;y:number;z:number};maximum:{x:number;y:number;z:number}}

export default function Scene({run,time,view,showCollisionGeometry}:{run:Run|null;time:number;view:string;showCollisionGeometry:boolean}){
 const host=useRef<HTMLDivElement>(null)
 const live=useRef({run,time,view,showCollisionGeometry})
 live.current={run,time,view,showCollisionGeometry}
 const sceneKey=JSON.stringify([run?.points,run?.mounts,run?.deck_metadata,run?.route?.source,run?.route?.segments,run?.route?.fixtures])

 useEffect(()=>{
  if(!host.current)return
  const element=host.current
  const scene=new THREE.Scene()
  scene.background=new THREE.Color('#132128')
  scene.fog=new THREE.Fog('#132128',750,1500)
  const camera=new THREE.PerspectiveCamera(36,1,1,2200)
  camera.position.set(530,420,570)
  const renderer=new THREE.WebGLRenderer({antialias:true})
  renderer.setPixelRatio(Math.min(devicePixelRatio,2))
  renderer.shadowMap.enabled=true
  renderer.shadowMap.type=THREE.PCFSoftShadowMap
  renderer.outputColorSpace=THREE.SRGBColorSpace
  renderer.toneMapping=THREE.ACESFilmicToneMapping
  renderer.toneMappingExposure=1.45
  element.appendChild(renderer.domElement)
  const controls=new OrbitControls(camera,renderer.domElement)
  controls.target.set(0,120,0)
  controls.enableDamping=true
  controls.minDistance=120
  controls.maxDistance=1000
  controls.maxPolarAngle=Math.PI*.49

  scene.add(new THREE.HemisphereLight('#d4edff','#253134',2.5))
  const light=new THREE.DirectionalLight('#fff5df',4)
  light.position.set(-200,500,250)
  light.castShadow=true
  light.shadow.mapSize.set(2048,2048)
  Object.assign(light.shadow.camera,{left:-450,right:450,top:450,bottom:-450,near:1,far:1000})
  light.shadow.bias=-.0004
  scene.add(light)
  const fill=new THREE.DirectionalLight('#79dff2',2)
  fill.position.set(200,180,-250)
  scene.add(fill)

  const material=(color:string,metalness=0,roughness=.5,opacity=1)=>new THREE.MeshStandardMaterial({color,metalness,roughness,transparent:opacity<1,opacity,depthWrite:opacity>=1})
  const teal=material('#168296',.65,.27)
  const black=material('#15252c',.5)
  const steel=material('#bbc4c5',.9,.24)
  const cream=material('#f1eee0',.05,.65)
  const white=material('#d9dfd9',.1,.36)
  const glass=material('#d6f2f2',.05,.16,.45)
  const tipGlass=material('#d8f1ef',.05,.2,.7)
  const dark=material('#182021')
  const orange=material('#f6b66e',.2,.35,.92)
  function box(parent:THREE.Object3D,w:number,h:number,d:number,x:number,y:number,z:number,mat:THREE.Material){const mesh=new THREE.Mesh(new THREE.BoxGeometry(w,h,d),mat);mesh.position.set(x,y,z);mesh.castShadow=true;mesh.receiveShadow=true;parent.add(mesh);return mesh}
  function cyl(parent:THREE.Object3D,r:number,h:number,x:number,y:number,z:number,mat:THREE.Material,r2=r,open=false){const mesh=new THREE.Mesh(new THREE.CylinderGeometry(r,r2,h,24,1,open),mat);mesh.position.set(x,y,z);mesh.castShadow=true;mesh.receiveShadow=true;parent.add(mesh);return mesh}
  function ring(parent:THREE.Object3D,inner:number,outer:number,x:number,y:number,z:number,mat:THREE.Material){const mesh=new THREE.Mesh(new THREE.RingGeometry(inner,outer,24),mat);mesh.rotation.x=-Math.PI/2;mesh.position.set(x,y,z);mesh.receiveShadow=true;parent.add(mesh);return mesh}
  function rail(x1:number,y1:number,z1:number,x2:number,y2:number,z2:number,r=4,mat=steel){const a=new THREE.Vector3(x1,y1,z1),b=new THREE.Vector3(x2,y2,z2);const mesh=cyl(scene,r,a.distanceTo(b),...a.clone().add(b).multiplyScalar(.5).toArray() as [number,number,number],mat);mesh.quaternion.setFromUnitVectors(new THREE.Vector3(0,1,0),b.sub(a).normalize());return mesh}
  const sceneLabels:THREE.Sprite[]=[]
  function label(parent:THREE.Object3D,text:string,x:number,y:number,z:number,size=20){const canvas=document.createElement('canvas');canvas.width=512;canvas.height=80;const context=canvas.getContext('2d')!;context.fillStyle='#dae9e9';context.font='500 29px sans-serif';context.textAlign='center';context.fillText(text,256,48);const sprite=new THREE.Sprite(new THREE.SpriteMaterial({map:new THREE.CanvasTexture(canvas),transparent:true,depthTest:false}));sprite.position.set(x,y,z);sprite.scale.set(size*5,size*.78,1);parent.add(sprite);sceneLabels.push(sprite);return sprite}

  const floorMaterial=material('#132128',.2,.9)
  box(scene,1400,4,1400,0,-8,0,floorMaterial)
  const grid=new THREE.GridHelper(1100,44,'#31494f','#24373d')
  grid.position.y=-5
  scene.add(grid)
  for(const x of [-169,169]){box(scene,18,28,365,x,15,0,steel);box(scene,20,9,370,x,33,0,black);for(const z of [-165,165])box(scene,30,13,32,x,0,z,black)}
  for(const z of [-178,178]){box(scene,356,38,11,0,22,z,teal);for(const x of [-154,154]){const bolt=cyl(scene,4,3,x,22,z+(z>0?7:-7),steel);bolt.rotation.x=Math.PI/2}}
  rail(-100,36,-163,-100,36,163)
  rail(100,36,-163,100,36,163)
  rail(0,32,-167,0,32,167,3)
  for(const x of [-166,166]){box(scene,12,241,52,x,153,-12,teal);box(scene,27,46,60,x,57,-12,cream);for(const y of [80,230,263]){const bolt=cyl(scene,4,4,x+8,y,10,steel);bolt.rotation.z=Math.PI/2}}
  rail(-162,255,-5,162,255,-5,6)
  rail(-162,207,-5,162,207,-5,6)
  rail(-162,231,-8,162,231,-8,3)
  box(scene,37,35,38,-191,231,-8,black)
  box(scene,12,33,36,-171,231,-8,steel)
  label(scene,'GENMITSU  /  3018',0,27,187,15)

  const bed=new THREE.Group()
  scene.add(bed)
  box(bed,300,8,180,0,55,0,steel)
  for(let x=-135;x<=135;x+=27)for(let z=-75;z<=75;z+=30)cyl(bed,1.7,1,x,59.5,z,dark)
  const points=run?.points??{}
  const fixture=(name:string)=>run?.route?.fixtures?.find(item=>item.name===name) as Bounds|undefined
  function boundsFor(key:string,height:number,length?:number,width?:number):Bounds|undefined{
   const exact=fixture(key)
   if(exact)return exact
   const values=points[key]??[]
   if(!values.length)return
   const minX=Math.min(...values.map(point=>point.x)),maxX=Math.max(...values.map(point=>point.x))
   const minY=Math.min(...values.map(point=>point.y)),maxY=Math.max(...values.map(point=>point.y))
   const top=Math.max(...values.map(point=>point.z))
   const xMargin=Math.max(6,((length??maxX-minX+20)-(maxX-minX))/2)
   const yMargin=Math.max(6,((width??maxY-minY+20)-(maxY-minY))/2)
   return {minimum:{x:minX-xMargin,y:minY-yMargin,z:top-height},maximum:{x:maxX+xMargin,y:maxY+yMargin,z:top}}
  }
  function framedTray(bounds:Bounds,mat:THREE.Material,wallHeight:number){
   const width=bounds.maximum.x-bounds.minimum.x,depth=bounds.maximum.y-bounds.minimum.y
   const centerX=(bounds.minimum.x+bounds.maximum.x)/2-145,centerZ=90-(bounds.minimum.y+bounds.maximum.y)/2
   const floor=3,wall=3,base=bounds.minimum.z
   box(bed,width,floor,depth,centerX,60+base+floor/2,centerZ,mat)
   const height=Math.min(bounds.maximum.z-base,wallHeight)
   box(bed,width,height,wall,centerX,60+base+height/2,90-bounds.minimum.y-wall/2,mat)
   box(bed,width,height,wall,centerX,60+base+height/2,90-bounds.maximum.y+wall/2,mat)
   box(bed,wall,height,Math.max(1,depth-2*wall),bounds.minimum.x-145+wall/2,60+base+height/2,centerZ,mat)
   box(bed,wall,height,Math.max(1,depth-2*wall),bounds.maximum.x-145-wall/2,60+base+height/2,centerZ,mat)
  }

  const wellMeshes=new Map<string,THREE.Mesh>()
  const tipMeshes=new Map<string,THREE.Object3D>()
  const collisionMeshes=new Map<string,{mesh:THREE.Mesh;marker?:THREE.Sprite}>()
  const routeLines:THREE.Line[]=[]
  const plateMeta=run?.deck_metadata?.plate
  const plateBounds=boundsFor('plate',plateMeta?.height??15,plateMeta?.length,plateMeta?.width)
  if(plateBounds)framedTray(plateBounds,cream,plateBounds.maximum.z-plateBounds.minimum.z)
  for(const point of points.plate??[]){
   const x=point.x-145,z=90-point.y
   ring(bed,3.2,4.4,x,60+point.z+.15,z,cream)
   const well=cyl(bed,3.05,1.2,x,60+point.z-1.9,z,material('#aebbb9',.05,.25))
   well.userData={filledY:60+point.z-.55,emptyY:60+point.z-1.9}
   wellMeshes.set(point.id,well)
  }

  const stockMeta=run?.deck_metadata?.stocks
  const vialHeight=stockMeta?.vial_height??45,vialRadius=(stockMeta?.vial_diameter??18)/2
  const stockBounds=boundsFor('stocks',vialHeight)
  if(stockBounds)framedTray(stockBounds,cream,Math.min(12,stockBounds.maximum.z-stockBounds.minimum.z))
  for(const point of points.stocks??[]){
   const x=point.x-145,z=90-point.y
   cyl(bed,vialRadius,vialHeight,x,60+point.z-vialHeight/2,z,glass,vialRadius*.92,true)
   ring(bed,vialRadius*.72,vialRadius,x,60+point.z+.1,z,white)
   const fluid=run?.fluids?.[point.id]
   if(fluid){
    const fillHeight=Math.max(5,(vialHeight-7)*Math.min(1,fluid.volume/(stockMeta?.working_volume_ul??4000)))
    cyl(bed,vialRadius-1.5,fillHeight,x,60+point.z-vialHeight+3+fillHeight/2,z,material(fluid.color,0,.28,.9))
   }
  }

  const sideExit=run?.deck_metadata?.tips.side_exit
  const rackInward=!!sideExit&&sideExit.exit_x<(points.tips?.[0]?.x??0)
  const tipLength=run?.deck_metadata?.tips.tip_length??25
  const tipBounds=boundsFor('tips',run?.deck_metadata?.tips.height??Math.max(20,tipLength),run?.deck_metadata?.tips.length,run?.deck_metadata?.tips.width)
  let disposed=false
  if(sideExit){
   const a1=points.tips?.find(point=>point.id==='tips.A1')
   const a2=points.tips?.find(point=>point.id==='tips.A2')
   const b1=points.tips?.find(point=>point.id==='tips.B1')
   if(a1&&a2&&b1){
    new STLLoader().load(new URL('./assets/ColorMatching_TipHolder.stl',import.meta.url).href,geometry=>{
     if(disposed){geometry.dispose();return}
     // The calibrated row basis reflects the nominal STL. Reverse each face
     // before rebuilding normals so the opaque mesh retains outward winding.
     geometry.applyMatrix4(new THREE.Matrix4().set(...rackSceneMatrix(a1,a2,b1)))
     reverseTriangleWinding(geometry.getAttribute('position').array)
     geometry.getAttribute('position').needsUpdate=true
     geometry.computeVertexNormals()
     const rackMaterial=cream.clone()
     const mesh=new THREE.Mesh(geometry,rackMaterial)
     mesh.castShadow=true
     mesh.receiveShadow=true
     bed.add(mesh)
    })
    label(bed,'A1 · first pickup',a1.x-145,60+a1.z+12,90-a1.y,9)
   }
  }else if(tipBounds){
   const photoRack=tipLength>=40 && Math.abs((tipBounds.maximum.x-tipBounds.minimum.x)-124)<8
   if(photoRack){
    // The saved Picus rack is a vertical open rack: the cream body stops
    // below a solid green 8 × 12 tray, leaving the 42 mm tips visible.
    const minX=tipBounds.minimum.x,maxX=tipBounds.maximum.x,minY=tipBounds.minimum.y,maxY=tipBounds.maximum.y
    const trayHeight=5
    // The saved pickup point is the tip mouth plane. Keep roughly 16 mm of
    // each 42 mm tip above the tray, matching the open rack in the reference
    // photo; the rack body itself remains a visual presentation estimate.
    const trayTop=tipBounds.maximum.z-16
    const bodyTop=trayTop-trayHeight
    const bodyHeight=Math.max(8,bodyTop-tipBounds.minimum.z)
    const rackBody=material('#f1eee0',.02,.42,.58)
    box(bed,maxX-minX,bodyHeight,maxY-minY,(minX+maxX)/2-145,60+tipBounds.minimum.z+bodyHeight/2,90-(minY+maxY)/2,rackBody)
    const tray=material('#078d4d',.05,.3)
    box(bed,maxX-minX,trayHeight,maxY-minY,(minX+maxX)/2-145,60+bodyTop+trayHeight/2,90-(minY+maxY)/2,tray)
    const hole=material('#116b4e',.05,.38)
    // Use the native calibrated points for the hole centers so the tray and
    // each consumable remain registered to the same 8 × 12 coordinates.
    for(const point of points.tips??[])
     cyl(bed,3.1,.22,point.x-145,60+trayTop+.12,90-point.y,hole,3.1)
   }else{
    framedTray(tipBounds,cream,Math.min(10,tipBounds.maximum.z-tipBounds.minimum.z))
    const minX=tipBounds.minimum.x,maxX=tipBounds.maximum.x,minY=tipBounds.minimum.y,maxY=tipBounds.maximum.y,top=tipBounds.maximum.z-2
    for(let column=0;column<=12;column++){const x=minX+(maxX-minX)*column/12;box(bed,1.3,2.5,maxY-minY,x-145,60+top,90-(minY+maxY)/2,cream)}
    for(let row=0;row<=8;row++){const y=minY+(maxY-minY)*row/8;box(bed,maxX-minX,2.5,1.3,(minX+maxX)/2-145,60+top,90-y,cream)}
   }
  }
  for(const point of points.tips??[]){
   const tipGroup=new THREE.Group()
   const tip=cyl(tipGroup,2.8,tipLength,0, -tipLength/2,0,tipGlass,.35)
   tip.castShadow=true
   // A small molded collar makes each clear 42 mm tip read as a separate
   // consumable, including the empty slot after a pickup.
   cyl(tipGroup,3.25,2.2,0,-1.1,0,tipGlass,3.25)
   tipGroup.position.set(point.x-145,60+point.z,90-point.y)
   bed.add(tipGroup)
   tipMeshes.set(point.id,tipGroup)
  }

  const wasteBounds=fixture('waste')
  const wasteLocation=run?.deck_metadata?.waste.location
  if(wasteLocation){
   const height=wasteBounds?wasteBounds.maximum.z-wasteBounds.minimum.z:run?.deck_metadata?.waste.height??40
   const radius=wasteBounds?Math.min(wasteBounds.maximum.x-wasteBounds.minimum.x,wasteBounds.maximum.y-wasteBounds.minimum.y)/2:10
   const bottom=wasteBounds?.minimum.z??wasteLocation.z-height
   const x=wasteLocation.x-145,z=90-wasteLocation.y
   cyl(bed,radius,height,x,60+bottom+height/2,z,cream,radius*.9,true)
   ring(bed,radius*.72,radius,x,60+bottom+height+.1,z,cream)
   cyl(bed,radius*.7,1,x,60+bottom+height-.2,z,dark)
  }

  for(const segment of run?.route?.segments??[]){
   const [a,b]=[segment.start,segment.end].map(([x,y,z])=>new THREE.Vector3(x-145,60+z,90-y))
   const axes=(segment.axes??'').toUpperCase()
   const line=new THREE.Line(new THREE.BufferGeometry().setFromPoints([a,b]),new THREE.LineBasicMaterial({color:axes.includes('Y')?'#9fd4ac':axes.includes('X')?'#ffb85c':'#8dc8e3',depthTest:false}))
   line.renderOrder=12
   bed.add(line)
   routeLines.push(line)
  }
  const collisionMaterial=new THREE.MeshBasicMaterial({color:'#ee8e62',transparent:true,opacity:.22,depthWrite:false,wireframe:true})
  for(const item of run?.route?.fixtures??[]){
   const {minimum,maximum}=item
   const mesh=box(bed,maximum.x-minimum.x,maximum.z-minimum.z,maximum.y-minimum.y,(minimum.x+maximum.x)/2-145,60+(minimum.z+maximum.z)/2,90-(minimum.y+maximum.y)/2,collisionMaterial)
   mesh.castShadow=false
   mesh.receiveShadow=false
   mesh.renderOrder=8
   const marker=item.name.includes('.tip.')?undefined:label(bed,item.name,(minimum.x+maximum.x)/2-145,60+maximum.z+5,90-(minimum.y+maximum.y)/2,5)
   collisionMeshes.set(item.name,{mesh,marker})
  }

  const carriage=new THREE.Group()
  scene.add(carriage)
  box(carriage,55,76,35,0,232,-15,teal)
  for(const x of [-16,16])cyl(carriage,3,155,x,210,9,steel)
  box(carriage,42,26,40,0,299,-5,black)
  box(carriage,44,5,42,0,283,-5,steel)
  const head=new THREE.Group()
  scene.add(head)
  box(head,43,47,20,0,48,3,teal)
  const mounts=run?.mounts??{pipette:{offset_x:0,offset_y:0,depth:0},camera:{offset_x:-25,offset_y:-28,depth:-18}}
  const pipette=new THREE.Group()
  head.add(pipette)
  pipette.position.set(mounts.pipette.offset_x,-mounts.pipette.depth,-mounts.pipette.offset_y)
  cyl(pipette,13,110,0,91,17,cream,15)
  cyl(pipette,14,10,0,151,17,white)
  box(pipette,19,31,3,0,122,31,dark)
  box(pipette,13,15,1,0,125,33,material('#6b8792',.1))
  cyl(pipette,5,36,0,18,17,cream,2.2)
  for(let index=0;index<5;index++)box(pipette,17,1,1,0,72+index*5,31,white)
  box(pipette,39,44,36,0,52,13,cream)
  box(pipette,57,16,47,0,51,10,teal)
  const attachedTip=cyl(pipette,2.2,25,0,-12.5,17,sideExit?orange:tipGlass,.3)
  attachedTip.visible=false
  const cam=new THREE.Group()
  head.add(cam)
  cam.position.set(mounts.camera.offset_x,-mounts.camera.depth,-mounts.camera.offset_y+17)
  box(cam,38,4,32,0,7,0,cream)
  box(cam,28,2,24,0,11,0,material('#204633',.25))
  cyl(cam,5,8,0,0,0,black)
  cyl(cam,3.4,1,0,-4.5,0,material('#244657',.8,.1))
  box(cam,6,5,4,9,15,0,steel)
  const fov=new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.ConeGeometry(24,45,4,1,true)),new THREE.LineBasicMaterial({color:'#f8b774',transparent:true,opacity:.2}))
  fov.position.y=-28
  fov.rotation.z=Math.PI
  cam.add(fov)
  const cordCurve=new THREE.CatmullRomCurve3([new THREE.Vector3(145,290,-25),new THREE.Vector3(40,345,-50),new THREE.Vector3(-100,295,-40),new THREE.Vector3(-155,160,-40)])
  scene.add(new THREE.Mesh(new THREE.TubeGeometry(cordCurve,30,1.8,6,false),black))
  const origin=cyl(bed,3,2,-145,62,90,orange)
  origin.name='Deck origin'

  const resize=()=>{const {width,height}=element.getBoundingClientRect();renderer.setSize(width,height);camera.aspect=width/height;camera.updateProjectionMatrix()}
  const observer=new ResizeObserver(resize)
  observer.observe(element)
  resize()
  let frame=0,lastView='orbit'
  function tick(){
   const state=stateAt(live.current.run,live.current.time)
   head.position.set(state.pose[0]-145,60+state.pose[2],-17)
   bed.position.z=state.pose[1]-90
   carriage.position.x=state.pose[0]-145
   attachedTip.visible=state.tip>0
   attachedTip.scale.y=state.tip/25
   attachedTip.position.y=-state.tip/2
   for(const [id,mesh] of wellMeshes){const value=state.wells[id];(mesh.material as THREE.MeshStandardMaterial).color.set(value?.color??'#aebbb9');mesh.position.y=value?mesh.userData.filledY:mesh.userData.emptyY}
   for(const [id,mesh] of tipMeshes)mesh.visible=!state.used.has(id)
   const photoMode=live.current.view==='photo'
   for(const label of sceneLabels)label.visible=!photoMode
   for(const line of routeLines)line.visible=!photoMode
   for(const [name,{mesh,marker}] of collisionMeshes){const visible=!photoMode&&live.current.showCollisionGeometry&&!(name.includes('.tip.')&&state.used.has(name.replace('.tip.','.')));mesh.visible=visible;if(marker)marker.visible=visible}
   fov.visible=!photoMode&&state.current?.kind==='capture'
   origin.visible=!photoMode
   grid.visible=!photoMode
   if(scene.background instanceof THREE.Color)scene.background.set(photoMode?'#b9b4a8':'#132128')
   scene.fog?.color.set(photoMode?'#b9b4a8':'#132128')
   floorMaterial.color.set(photoMode?'#8c8a82':'#132128')
   renderer.toneMappingExposure=photoMode?1.1:1.45
   if(live.current.view!==lastView){lastView=live.current.view;camera.position.set(...(lastView==='photo'?[10,355,680]:lastView==='rack'?(rackInward?[-170,240,310]:[275,260,300]):lastView==='top'?[0,680,1]:lastView==='front'?[0,220,660]:[530,420,570]) as [number,number,number]);controls.target.set(...(lastView==='photo'?[0,120,0]:lastView==='rack'?(rackInward?[35,120,0]:[110,130,0]):[0,120,0]) as [number,number,number])}
   controls.update()
   renderer.render(scene,camera)
   frame=requestAnimationFrame(tick)
  }
  tick()
  return()=>{
   disposed=true
   cancelAnimationFrame(frame)
   observer.disconnect()
   controls.dispose()
   scene.traverse(object=>{const mesh=object as THREE.Mesh;if(mesh.geometry)mesh.geometry.dispose();if(mesh.material){for(const item of Array.isArray(mesh.material)?mesh.material:[mesh.material]){if('map' in item)(item as THREE.MeshBasicMaterial).map?.dispose();item.dispose()}}})
   renderer.dispose()
   renderer.domElement.remove()
  }
 },[sceneKey])

 return <div className="scene-canvas" ref={host} aria-label="Interactive 3D Genmitsu simulator. Drag to orbit, scroll to zoom."/>
}
