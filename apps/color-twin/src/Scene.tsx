import { useEffect, useRef } from 'react'
import * as THREE from 'three'
import { STLLoader } from 'three/addons/loaders/STLLoader.js'
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'
import { stateAt, type Run } from './types'

export default function Scene({run,time,view}:{run:Run|null;time:number;view:string}){
 const host=useRef<HTMLDivElement>(null);const live=useRef({run,time,view});live.current={run,time,view}
 const sceneKey=JSON.stringify([run?.points,run?.mounts,run?.deck_metadata])
 useEffect(()=>{
 if(!host.current)return
 const element=host.current;const scene=new THREE.Scene();scene.background=new THREE.Color('#132128');scene.fog=new THREE.Fog('#132128',750,1500)
 const camera=new THREE.PerspectiveCamera(36,1,1,2200);camera.position.set(530,420,570)
 const renderer=new THREE.WebGLRenderer({antialias:true});renderer.setPixelRatio(Math.min(devicePixelRatio,2));renderer.shadowMap.enabled=true;renderer.shadowMap.type=THREE.PCFSoftShadowMap;renderer.outputColorSpace=THREE.SRGBColorSpace;renderer.toneMapping=THREE.ACESFilmicToneMapping;renderer.toneMappingExposure=1.45;element.appendChild(renderer.domElement)
 const controls=new OrbitControls(camera,renderer.domElement);controls.target.set(0,120,0);controls.enableDamping=true;controls.minDistance=120;controls.maxDistance=1000;controls.maxPolarAngle=Math.PI*.49
 scene.add(new THREE.HemisphereLight('#d4edff','#253134',2.5));const light=new THREE.DirectionalLight('#fff5df',4);light.position.set(-200,500,250);light.castShadow=true;light.shadow.mapSize.set(2048,2048);Object.assign(light.shadow.camera,{left:-450,right:450,top:450,bottom:-450,near:1,far:1000});light.shadow.bias=-.0004;scene.add(light)
 const fill=new THREE.DirectionalLight('#79dff2',2);fill.position.set(200,180,-250);scene.add(fill)
 const material=(c:string,metal=0,rough=.5,opacity=1)=>new THREE.MeshStandardMaterial({color:c,metalness:metal,roughness:rough,transparent:opacity<1,opacity})
 const teal=material('#168296',.65,.27),black=material('#15252c',.5),steel=material('#bbc4c5',.9,.24),cream=material('#f1eee0',.05,.65),white=material('#d9dfd9',.1,.36),glass=material('#c9edf3',0,.12,.3),dark=material('#182021'),orange=material('#f6b66e',.2)
 function box(parent:THREE.Object3D,w:number,h:number,d:number,x:number,y:number,z:number,mat:THREE.Material){const m=new THREE.Mesh(new THREE.BoxGeometry(w,h,d),mat);m.position.set(x,y,z);m.castShadow=true;m.receiveShadow=true;parent.add(m);return m}
 function cyl(parent:THREE.Object3D,r:number,h:number,x:number,y:number,z:number,mat:THREE.Material,r2=r){const m=new THREE.Mesh(new THREE.CylinderGeometry(r,r2,h,24),mat);m.position.set(x,y,z);m.castShadow=true;m.receiveShadow=true;parent.add(m);return m}
 function rail(x1:number,y1:number,z1:number,x2:number,y2:number,z2:number,r=4,mat=steel){const a=new THREE.Vector3(x1,y1,z1),b=new THREE.Vector3(x2,y2,z2);const m=cyl(scene,r,a.distanceTo(b),...a.clone().add(b).multiplyScalar(.5).toArray() as [number,number,number],mat);m.quaternion.setFromUnitVectors(new THREE.Vector3(0,1,0),b.sub(a).normalize());return m}
 function label(text:string,x:number,y:number,z:number,size=20){const c=document.createElement('canvas');c.width=512;c.height=80;const ctx=c.getContext('2d')!;ctx.fillStyle='#dae9e9';ctx.font='500 29px sans-serif';ctx.textAlign='center';ctx.fillText(text,256,48);const sprite=new THREE.Sprite(new THREE.SpriteMaterial({map:new THREE.CanvasTexture(c),transparent:true,depthTest:false}));sprite.position.set(x,y,z);sprite.scale.set(size*5,size*.78,1);scene.add(sprite);return sprite}
 box(scene,1400,4,1400,0,-8,0,material('#132128',.2,.9));const grid=new THREE.GridHelper(1100,44,'#31494f','#24373d');grid.position.y=-5;scene.add(grid)
 for(const x of [-169,169]){box(scene,18,28,365,x,15,0,steel);box(scene,20,9,370,x,33,0,black);for(const z of [-165,165])box(scene,30,13,32,x,0,z,black)}
 for(const z of [-178,178]){box(scene,356,38,11,0,22,z,teal);for(const x of [-154,154]){const bolt=cyl(scene,4,3,x,22,z+(z>0?7:-7),steel);bolt.rotation.x=Math.PI/2}}
 rail(-100,36,-163,-100,36,163);rail(100,36,-163,100,36,163);rail(0,32,-167,0,32,167,3)
 for(const x of [-166,166]){box(scene,12,241,52,x,153,-12,teal);box(scene,27,46,60,x,57,-12,cream);for(const y of [80,230,263]){const bolt=cyl(scene,4,4,x+8,y,10,steel);bolt.rotation.z=Math.PI/2}}
 rail(-162,255,-5,162,255,-5,6);rail(-162,207,-5,162,207,-5,6);rail(-162,231,-8,162,231,-8,3)
 box(scene,37,35,38,-191,231,-8,black);box(scene,12,33,36,-171,231,-8,steel)
 label('GENMITSU  /  3018',0,27,187,15)
 const bed=new THREE.Group();scene.add(bed);box(bed,300,8,180,0,55,0,steel)
 for(let x=-135;x<=135;x+=27)for(let z=-75;z<=75;z+=30)cyl(bed,1.7,1,x,59.5,z,dark)
 const points=run?.points??{};const wellMeshes=new Map<string,THREE.Mesh>();const tipMeshes=new Map<string,THREE.Mesh>();
 function tray(key:string,height:number,mat:THREE.Material){const ps=points[key]??[];if(!ps.length)return;const xs=ps.map(p=>p.x-145),zs=ps.map(p=>90-p.y);const minx=Math.min(...xs),maxx=Math.max(...xs),minz=Math.min(...zs),maxz=Math.max(...zs);box(bed,maxx-minx+24,height,maxz-minz+23,(minx+maxx)/2,60+height/2,(minz+maxz)/2,mat);return {minx,maxx,minz,maxz}}
 tray('plate',(run?.deck_metadata?.plate.height??15)-5,cream)
 for(const p of points.plate??[]){cyl(bed,3.8,7,p.x-145,60+p.z-3,90-p.y,glass);const well=cyl(bed,3,1,p.x-145,60+p.z-7,90-p.y,material('#c5d5d6',.05,.23));well.userData.surface=60+p.z-4;wellMeshes.set(p.id,well)}
 const sideExit=run?.deck_metadata?.tips.side_exit
 const rackInward=!!sideExit&&sideExit.exit_x<(points.tips?.[0]?.x??0)
 const tipLength=run?.deck_metadata?.tips.tip_length??25
 if(sideExit){
   const location=run?.deck_metadata?.tips.location??{x:150,y:20,z:0}
   new STLLoader().load(new URL('./assets/ColorMatching_TipHolder.stl',import.meta.url).href,geometry=>{
     // CAD base Y=-63; open end Z=-120. Match the configured exit side.
     const inward=sideExit.exit_x<location.x
     geometry.applyMatrix4(inward
       ? new THREE.Matrix4().set(0,0,1,location.x-25,0,1,0,123+location.z,-1,0,0,90-location.y,0,0,0,1)
       : new THREE.Matrix4().set(0,0,-1,location.x-145,0,1,0,123+location.z,1,0,0,6-location.y,0,0,0,1))
     geometry.computeVertexNormals();const mesh=new THREE.Mesh(geometry,cream);mesh.castShadow=true;mesh.receiveShadow=true;bed.add(mesh)
   })
   const a1=points.tips?.[0];if(a1)bed.add(label('A1',a1.x-145,60+a1.z+12,90-a1.y,7))
   const p=points.tips?.[11];if(p){
     bed.add(label('A12 · first pickup',p.x-145,60+p.z+12,90-p.y,9))
     const bottom=p.z-tipLength;const lift=bottom+sideExit.lift_mm
     const path=[new THREE.Vector3(p.x-145,60+bottom,90-p.y),new THREE.Vector3(p.x-145,60+lift,90-p.y),new THREE.Vector3(sideExit.exit_x-145,60+lift,90-p.y)]
     const line=new THREE.Line(new THREE.BufferGeometry().setFromPoints(path),new THREE.LineBasicMaterial({color:'#ffb85c',depthTest:false}));line.renderOrder=10;bed.add(line)
   }
   const waste=run?.deck_metadata?.waste.location
   if(waste){cyl(bed,11,40,waste.x-145,80,90-waste.y,cream);cyl(bed,9,1,waste.x-145,101,90-waste.y,dark)}
 }else{
   const rack=tray('tips',(points.tips?.[0]?.z??45)-10,cream)
   if(rack)for(let row=0;row<9;row++)box(bed,rack.maxx-rack.minx+24,11,1.3,(rack.minx+rack.maxx)/2,60+(points.tips?.[0]?.z??45)-4,rack.minz-5+row*9,cream)
 }
 for(const p of points.tips??[]){const tip=cyl(bed,2.8,tipLength,p.x-145,60+p.z-tipLength/2,90-p.y,glass,.3);tipMeshes.set(p.id,tip)}
 tray('stocks',24,cream)
 const stockColors=['#df244b','#f1ca25','#246ddd']
 for(const [i,p] of (points.stocks??[]).entries()){cyl(bed,8,27,p.x-145,60+p.z-12,90-p.y,glass);if(i<3)cyl(bed,6.5,17,p.x-145,60+p.z-17,90-p.y,material(stockColors[i],0,.3,.85));cyl(bed,8,3,p.x-145,60+p.z+3,90-p.y,white)}
 const carriage=new THREE.Group();scene.add(carriage);box(carriage,55,76,35,0,232,-15,teal)
 for(const x of [-16,16])cyl(carriage,3,155,x,210,9,steel)
 box(carriage,42,26,40,0,299,-5,black);box(carriage,44,5,42,0,283,-5,steel)
 const head=new THREE.Group();scene.add(head);box(head,43,47,20,0,48,3,teal)
 const mounts=run?.mounts??{pipette:{offset_x:0,offset_y:0,depth:0},camera:{offset_x:-25,offset_y:-28,depth:-18}}
 const pipette=new THREE.Group();head.add(pipette);pipette.position.set(mounts.pipette.offset_x,-mounts.pipette.depth,-mounts.pipette.offset_y)
 cyl(pipette,13,110,0,91,17,cream,15);cyl(pipette,14,10,0,151,17,white);box(pipette,19,31,3,0,122,31,dark);box(pipette,13,15,1,0,125,33,material('#6b8792',.1));cyl(pipette,5,36,0,18,17,cream,2.2)
 for(let j=0;j<5;j++)box(pipette,17,1,1,0,72+j*5,31,white)
 box(pipette,39,44,36,0,52,13,cream);box(pipette,57,16,47,0,51,10,teal)
 const tip=cyl(pipette,2.2,25,0,-12.5,17,sideExit?orange:glass,.3);tip.visible=false
 const cam=new THREE.Group();head.add(cam);cam.position.set(mounts.camera.offset_x,-mounts.camera.depth,-mounts.camera.offset_y+17)
 box(cam,38,4,32,0,7,0,cream);box(cam,28,2,24,0,11,0,material('#204633',.25));cyl(cam,5,8,0,0,0,black);cyl(cam,3.4,1,0,-4.5,0,material('#244657',.8,.1));box(cam,6,5,4,9,15,0,steel)
 const fov=new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.ConeGeometry(24,45,4,1,true)),new THREE.LineBasicMaterial({color:'#f8b774',transparent:true,opacity:.2}));fov.position.y=-28;fov.rotation.z=Math.PI;cam.add(fov)
 const cordCurve=new THREE.CatmullRomCurve3([new THREE.Vector3(145,290,-25),new THREE.Vector3(40,345,-50),new THREE.Vector3(-100,295,-40),new THREE.Vector3(-155,160,-40)]);scene.add(new THREE.Mesh(new THREE.TubeGeometry(cordCurve,30,1.8,6,false),black))
 const origin=cyl(bed,3,2,-145,62,90,orange);origin.name='Deck origin'
 const resize=()=>{const {width,height}=element.getBoundingClientRect();renderer.setSize(width,height);camera.aspect=width/height;camera.updateProjectionMatrix()};const observer=new ResizeObserver(resize);observer.observe(element);resize()
 let frame=0,lastView='orbit';function tick(){const state=stateAt(live.current.run,live.current.time);head.position.set(state.pose[0]-145,60+state.pose[2],-17);bed.position.z=state.pose[1]-90;carriage.position.x=state.pose[0]-145;tip.visible=state.tip>0;tip.scale.y=state.tip/25;tip.position.y=-state.tip/2
 for(const [id,mesh]of wellMeshes){(mesh.material as THREE.MeshStandardMaterial).color.set(state.wells[id]?.color??'#c5d5d6');mesh.position.y=mesh.userData.surface-(state.wells[id]?0:3)}
 for(const [id,mesh]of tipMeshes)mesh.visible=!state.used.has(id);
 fov.visible=state.current?.kind==='capture';
 if(live.current.view!==lastView){lastView=live.current.view;camera.position.set(...(lastView==='rack'?(rackInward?[-170,240,310]:[275,260,300]):lastView==='top'?[0,680,1]:lastView==='front'?[0,220,660]:[530,420,570]) as [number,number,number]);controls.target.set(...(lastView==='rack'?(rackInward?[35,120,0]:[110,130,0]):[0,120,0]) as [number,number,number])}
 controls.update();renderer.render(scene,camera);frame=requestAnimationFrame(tick)}tick()
 return()=>{cancelAnimationFrame(frame);observer.disconnect();controls.dispose();scene.traverse(o=>{const mesh=o as THREE.Mesh;if(mesh.geometry)mesh.geometry.dispose();if(mesh.material){for(const m of Array.isArray(mesh.material)?mesh.material:[mesh.material]){if('map'in m)(m as THREE.MeshBasicMaterial).map?.dispose();m.dispose()}}});renderer.dispose();renderer.domElement.remove()}
 },[sceneKey])
 return <div className="scene-canvas" ref={host} aria-label="Interactive 3D Genmitsu simulator. Drag to orbit, scroll to zoom." />
}
