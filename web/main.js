import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { GTAOPass } from 'three/addons/postprocessing/GTAOPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import { RectAreaLightUniformsLib } from 'three/addons/lights/RectAreaLightUniformsLib.js';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------
const WALL_T = 0.1;          // wall thickness (outward), m
const LAYER_GHOST = 1;       // near walls/ceiling: cast shadows, not drawn
const LAYER_PROBE = 2;       // seen only by the environment probe (sky behind the window)
const CEIL_COLOR = 0xfff0dc; // ~3500 K LED
const DOWNLIGHT = 'UNIB1501004';
const MIRROR_CABINET = 'INK_1105364'; // BRAUER Impress Deluxe spiegelkast 120x70
// Impress Deluxe: indirect LED strip set into the underside, lighting the basin. 4000 K per the spec;
// strip length and set-back are not published, so they are estimated.
const LED = { color: 0xffeedd, length: 1.12, width: 0.012, setback: 0.025, intensity: 40 };

// Render state. Declared first: texture callbacks fire while the module is still awaiting.
let dirty = true;
let warmup = 30; // keep drawing the first frames while textures and shaders settle
const status = document.getElementById("status");
status.textContent = "Laden…";
function markDirty() { dirty = true; }
const DAY_COLOR = 0xf4f7ff;  // overcast daylight
const ENV_INTENSITY = 0.8;    // probe = captured room radiance (two bounces)

const [scene3, palette] = await Promise.all([
  fetch('scene.json').then(r => r.json()),
  fetch('textures/palette.json').then(r => r.json()),
]);

// ---------------------------------------------------------------------------
// Renderer, scene, camera
// ---------------------------------------------------------------------------
const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setSize(innerWidth, innerHeight);
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.NeutralToneMapping; // keeps material colours close to their albedo
renderer.toneMappingExposure = 0.8;
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
document.body.appendChild(renderer.domElement);
RectAreaLightUniformsLib.init();

const scene = new THREE.Scene();
scene.background = new THREE.Color(0xe9e7e3);

const [RW, RH, RD] = scene3.room.size;
const center = new THREE.Vector3(RW / 2, RH / 2, RD / 2);

const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 60);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableRotate = false;
controls.enableDamping = true;
controls.zoomToCursor = true;
controls.target.copy(center).setY(RH * 0.42);
controls.minZoom = 0.6;
controls.maxZoom = 6;

// ---------------------------------------------------------------------------
// Materials
// ---------------------------------------------------------------------------
const texLoader = new THREE.TextureLoader();
const maxAniso = renderer.capabilities.getMaxAnisotropy();
function tex(url, color = true) {
  const t = texLoader.load(url, () => markDirty());
  t.colorSpace = color ? THREE.SRGBColorSpace : THREE.NoColorSpace;
  t.wrapS = t.wrapT = THREE.RepeatWrapping;
  t.anisotropy = maxAniso;
  // Tile size is baked into the UVs (boxUV), not texture.repeat: the path tracer ignores repeat.
  return t;
}

// Vertical flutes for the ribbed shower glass, as a normal map.
function flutedNormalMap(pitchPx = 16) {
  const c = document.createElement('canvas'); c.width = 256; c.height = 4;
  const g = c.getContext('2d'), img = g.createImageData(256, 4);
  for (let x = 0; x < 256; x++) {
    const s = Math.sin(((x % pitchPx) / pitchPx) * Math.PI * 2);
    const nx = s * 0.55, nz = Math.sqrt(1 - nx * nx);
    for (let y = 0; y < 4; y++) {
      const i = (y * 256 + x) * 4;
      img.data[i] = (nx * 0.5 + 0.5) * 255; img.data[i + 1] = 128; img.data[i + 2] = nz * 255; img.data[i + 3] = 255;
    }
  }
  g.putImageData(img, 0, 0);
  const t = new THREE.CanvasTexture(c);
  t.wrapS = t.wrapT = THREE.RepeatWrapping;
  return t;
}

const tileLight = tex('textures/tile_light.jpg');
const tileTaupe = tex('textures/tile_taupe.jpg');
const woodMap = tex('textures/wood.jpg');

const M = {
  tile_light: new THREE.MeshPhysicalMaterial({ map: tileLight, roughness: 0.42, bumpMap: tileLight, bumpScale: 0.6,
    specularIntensity: 0.6 }),
  tile_taupe: new THREE.MeshPhysicalMaterial({ map: tileTaupe, roughness: 0.48, bumpMap: tileTaupe, bumpScale: 0.6,
    specularIntensity: 0.6 }),
  plaster: new THREE.MeshStandardMaterial({ color: 0xf1efeb, roughness: 0.95 }),
  // Cut faces of the dollhouse section: unlit, so they read as a clean architectural cut.
  section: new THREE.MeshBasicMaterial({ color: 0xb4b0aa }),
  wood: new THREE.MeshPhysicalMaterial({ map: woodMap, roughness: 0.58, bumpMap: woodMap, bumpScale: 1.2,
    clearcoat: 0.15, clearcoatRoughness: 0.5 }),
  // Brushed gunmetal PVD. The photo median (palette.gunmetal) is the metal *as seen*, i.e. its
  // reflectance times a dim surrounding; as F0 that is too dark, so the base is lifted.
  // No anisotropy: without mesh tangents three.js produces NaNs with it.
  gunmetal: new THREE.MeshPhysicalMaterial({ color: new THREE.Color(palette.gunmetal).multiplyScalar(1.75),
    metalness: 1, roughness: 0.34 }),
  steel: new THREE.MeshStandardMaterial({ color: 0xcfcfcf, metalness: 1, roughness: 0.22 }),
  aluminium: new THREE.MeshStandardMaterial({ color: 0xbdbdbd, metalness: 1, roughness: 0.38 }),
  grey_matte: new THREE.MeshStandardMaterial({ color: 0x3b3a38, roughness: 0.7 }),
  black_matte: new THREE.MeshStandardMaterial({ color: 0x141414, roughness: 0.55 }),
  paint_white: new THREE.MeshStandardMaterial({ color: 0xeeece8, roughness: 0.6 }),
  mineral_white: new THREE.MeshPhysicalMaterial({ color: 0xf3f2ef, roughness: 0.5, specularIntensity: 0.5 }),
  ceramic_white: new THREE.MeshPhysicalMaterial({ color: 0xf5f5f3, roughness: 0.18, clearcoat: 1,
    clearcoatRoughness: 0.04 }),
  glass: new THREE.MeshPhysicalMaterial({ color: 0xf2f7f5, metalness: 0, roughness: 0.04, transmission: 1,
    thickness: 0.008, ior: 1.5, normalMap: flutedNormalMap(), normalScale: new THREE.Vector2(0.8, 0.8),
    side: THREE.DoubleSide }),
  window_glass: new THREE.MeshPhysicalMaterial({ color: 0xffffff, roughness: 0.0, transmission: 1, thickness: 0.004,
    ior: 1.5 }),
  mirror: new THREE.MeshStandardMaterial({ color: 0xf6f6f6, metalness: 1, roughness: 0.0 }),
  light_diffuser: new THREE.MeshStandardMaterial({ color: 0xffffff, emissive: CEIL_COLOR, emissiveIntensity: 6 }),
  led_strip: new THREE.MeshStandardMaterial({ color: 0xffffff, emissive: LED.color, emissiveIntensity: 5 }),
  plant_pot: new THREE.MeshStandardMaterial({ color: 0x8b847a, roughness: 0.9 }),
  plant_leaf: new THREE.MeshStandardMaterial({ color: 0x4c6a3b, roughness: 0.6, side: THREE.DoubleSide }),
  sky: new THREE.MeshBasicMaterial({ color: new THREE.Color(DAY_COLOR).multiplyScalar(3) }),
};
const TEX_SIZE = {
  tile_light: palette.tiles.light.size_mm.map(v => v / 1000),
  tile_taupe: palette.tiles.taupe.size_mm.map(v => v / 1000),
  wood: palette.wood_tile_mm.map(v => v / 1000),
  glass: [0.16, 100], // 256 px = 160 mm -> 10 mm flutes, constant vertically
};
const texSize = key => TEX_SIZE[key] ?? [1, 1];
const BOXUV_LOCAL = new Set(['wood', 'glass']); // product-local projection (tiles use world projection)

// Box-projected UVs in metres, using the given matrix (world or product-local).
function boxUV(geom, matrix, size = [1, 1]) {
  const pos = geom.attributes.position, nor = geom.attributes.normal;
  const nm = new THREE.Matrix3().getNormalMatrix(matrix);
  const uv = new Float32Array(pos.count * 2);
  const p = new THREE.Vector3(), n = new THREE.Vector3();
  for (let i = 0; i < pos.count; i++) {
    p.fromBufferAttribute(pos, i).applyMatrix4(matrix);
    n.fromBufferAttribute(nor, i).applyMatrix3(nm);
    const ax = Math.abs(n.x), ay = Math.abs(n.y), az = Math.abs(n.z);
    let u, v;
    if (ay >= ax && ay >= az) { u = p.x; v = p.z; }
    else if (ax >= az) { u = p.z; v = p.y; }
    else { u = p.x; v = p.y; }
    uv[i * 2] = u / size[0]; uv[i * 2 + 1] = v / size[1];
  }
  geom.setAttribute('uv', new THREE.BufferAttribute(uv, 2));
}

function shadowy(obj) {
  obj.traverse(o => { if (o.isMesh) { o.castShadow = true; o.receiveShadow = true; } });
  return obj;
}

// ---------------------------------------------------------------------------
// Room shell
// ---------------------------------------------------------------------------
const room = new THREE.Group();
scene.add(room);
const walls = {}; // id -> { mesh, normal }

{ // floor (slab with a section edge)
  const g = new THREE.BoxGeometry(RW + 2 * WALL_T, 0.06, RD + 2 * WALL_T);
  g.translate(RW / 2, -0.03, RD / 2);
  boxUV(g, new THREE.Matrix4(), texSize('tile_taupe'));
  const floor = new THREE.Mesh(g, [M.section, M.section, M.tile_taupe, M.section, M.section, M.section]);
  floor.receiveShadow = true;
  room.add(floor);
}

for (const w of scene3.walls) {
  const a = new THREE.Vector3(...w.a), b = new THREE.Vector3(...w.b);
  const L = a.distanceTo(b);
  const shape = new THREE.Shape();
  shape.moveTo(-WALL_T, 0); shape.lineTo(L + WALL_T, 0); shape.lineTo(L + WALL_T, RH); shape.lineTo(-WALL_T, RH);
  shape.closePath();
  for (const h of w.holes) {
    const hole = new THREE.Path();
    hole.moveTo(h.u0 / 1000, h.z0 / 1000 + (h.z0 === 0 ? 0.0005 : 0)); hole.lineTo(h.u1 / 1000, h.z0 / 1000);
    hole.lineTo(h.u1 / 1000, h.z1 / 1000); hole.lineTo(h.u0 / 1000, h.z1 / 1000); hole.closePath();
    shape.holes.push(hole);
  }
  const g = new THREE.ExtrudeGeometry(shape, { depth: WALL_T, bevelEnabled: false });
  g.translate(0, 0, -WALL_T); // inner face at local z = 0, wall grows outward
  const x = b.clone().sub(a).normalize(), y = new THREE.Vector3(0, 1, 0), z = new THREE.Vector3().crossVectors(x, y);
  const m = new THREE.Matrix4().makeBasis(x, y, z).setPosition(a);
  g.applyMatrix4(m);
  boxUV(g, new THREE.Matrix4(), texSize(w.finish));
  const finish = M[w.finish];
  const mesh = new THREE.Mesh(g, [finish, M.section]); // caps (faces) tiled, cut edges as section
  shadowy(mesh);
  room.add(mesh);
  walls[w.id] = { mesh, normal: new THREE.Vector3(...w.normal), holes: w.holes, a, x, z, extras: [] };
}

const ceiling = new THREE.Mesh(new THREE.BoxGeometry(RW + 2 * WALL_T, 0.05, RD + 2 * WALL_T), M.plaster);
ceiling.position.set(RW / 2, RH + 0.025, RD / 2);
shadowy(ceiling);
ceiling.layers.set(LAYER_GHOST);
room.add(ceiling);

// Built-out walls (ombouw / voorzetwand). BoxGeometry groups: +x, -x, +y, -y, +z(back), -z(front).
for (const bx of scene3.boxes) {
  const g = new THREE.BoxGeometry(...bx.size);
  // The back face sits against a wall; when that wall is cut away it shows as a section.
  const mesh = new THREE.Mesh(g, ['px', 'nx', 'top', 'bottom', 'back', 'front']
    .map(f => (f === 'back' ? M.section : M[bx.faces[f]])));
  mesh.position.set(...bx.center);
  mesh.rotation.y = bx.rotY;
  mesh.updateMatrixWorld();
  boxUV(g, mesh.matrixWorld, texSize('tile_light')); // all tiled faces of built walls use the light tile
  shadowy(mesh);
  room.add(mesh);
}

// Window: frame in the hole, glass, bright sky behind it.
const windowLights = [];
for (const w of Object.values(walls)) {
  for (const h of w.holes) {
    if (h.code !== 'XXXX999910002') continue;
    const u0 = h.u0 / 1000, u1 = h.u1 / 1000, z0 = h.z0 / 1000, z1 = h.z1 / 1000;
    const cw = u1 - u0, ch = z1 - z0, fr = 0.05;
    const grp = new THREE.Group();
    grp.matrixAutoUpdate = false;
    grp.matrix.makeBasis(w.x, new THREE.Vector3(0, 1, 0), w.z).setPosition(w.a);
    const add = (geo, mat, px, py, pz) => { const m = new THREE.Mesh(geo, mat); m.position.set(px, py, pz); grp.add(shadowy(m)); return m; };
    const depth = 0.07, zc = -WALL_T * 0.55;
    add(new THREE.BoxGeometry(cw, fr, depth), M.paint_white, (u0 + u1) / 2, z0 + fr / 2, zc);
    add(new THREE.BoxGeometry(cw, fr, depth), M.paint_white, (u0 + u1) / 2, z1 - fr / 2, zc);
    add(new THREE.BoxGeometry(fr, ch, depth), M.paint_white, u0 + fr / 2, (z0 + z1) / 2, zc);
    add(new THREE.BoxGeometry(fr, ch, depth), M.paint_white, u1 - fr / 2, (z0 + z1) / 2, zc);
    add(new THREE.BoxGeometry(fr * 0.8, ch, depth * 0.8), M.paint_white, (u0 + u1) / 2, (z0 + z1) / 2, zc);
    const glass = add(new THREE.BoxGeometry(cw, ch, 0.006), M.window_glass, (u0 + u1) / 2, (z0 + z1) / 2, zc);
    glass.castShadow = false;
    // window sill (tiled reveal is the wall cut; add a stone sill)
    add(new THREE.BoxGeometry(cw + 0.04, 0.02, WALL_T * 0.5 + 0.02), M.mineral_white, (u0 + u1) / 2, z0 + 0.01, -WALL_T * 0.25 + 0.01);
    const sky = new THREE.Mesh(new THREE.PlaneGeometry(cw * 4, ch * 6), M.sky);
    sky.position.set((u0 + u1) / 2, (z0 + z1) / 2, -1.2);
    sky.layers.set(LAYER_PROBE);
    grp.add(sky);
    // Soft daylight from the opening
    const area = new THREE.RectAreaLight(DAY_COLOR, 14, cw - 2 * fr, ch - 2 * fr);
    area.position.set((u0 + u1) / 2, (z0 + z1) / 2, -0.005);
    area.lookAt((u0 + u1) / 2, (z0 + z1) / 2, 1);
    grp.add(area);
    grp.updateMatrixWorld(true);
    room.add(grp);
    w.extras.push(...grp.children.filter(c => c.isMesh && c !== sky));
    // Sun-ish key through the window, for a soft patch of light and real shadows.
    const sun = new THREE.DirectionalLight(DAY_COLOR, 2.2);
    const mid = new THREE.Vector3((u0 + u1) / 2, (z0 + z1) / 2, 0).applyMatrix4(grp.matrix);
    sun.position.copy(mid).addScaledVector(w.z, -4).add(new THREE.Vector3(-1.2, 3.2, 0));
    sun.target.position.copy(mid).addScaledVector(w.z, 1.4).setY(0);
    sun.castShadow = true;
    sun.shadow.mapSize.set(2048, 2048);
    sun.shadow.radius = 6;
    sun.shadow.bias = -0.0004;
    sun.shadow.normalBias = 0.02;
    const sc = sun.shadow.camera; sc.left = -2.5; sc.right = 2.5; sc.top = 2.5; sc.bottom = -2.5; sc.near = 0.5; sc.far = 12;
    sc.layers.enable(LAYER_GHOST);
    scene.add(sun, sun.target);
    windowLights.push(area, sun);
  }
}

// ---------------------------------------------------------------------------
// Products
// ---------------------------------------------------------------------------
const loader = new GLTFLoader();
const modelCache = {};
function loadModel(code) {
  return (modelCache[code] ??= loader.loadAsync(`models/${code}.glb`).then(g => g.scene));
}

const products = new THREE.Group();
scene.add(products);
// Besides the design's ceiling spots, a single fixture in the centre of the ceiling (for comparison).
const items = [
  ...scene3.items,
  { code: DOWNLIGHT, center: true, pos: [RW / 2, RH, RD / 2], rotY: 0, scale: [1, 1, 1],
    materials: { Body: 'steel', Chrome: 'steel' } },
];
const roots = {}; // code -> [root]
const spotFixtures = [];
let centerFixture = null;
await Promise.all(items.map(async it => {
  const src = await loadModel(it.code);
  const root = new THREE.Group();
  root.position.set(...it.pos);
  root.rotation.y = it.rotY;
  root.scale.set(...it.scale);
  const inst = src.clone(true);
  root.add(inst);
  root.updateMatrixWorld(true);
  const scaleOnly = new THREE.Matrix4().makeScale(...it.scale);
  inst.traverse(o => {
    if (!o.isMesh) return;
    const slot = o.name.split('__')[0];
    const key = it.materials[slot] ?? scene3.slotMaterials[slot] ?? 'paint_white';
    o.material = M[key] ?? M.paint_white;
    if (BOXUV_LOCAL.has(key)) {
      o.geometry = o.geometry.clone();
      // product-local frame (with scale), so wood grain follows the cabinet width
      const local = scaleOnly.clone().multiply(new THREE.Matrix4().copy(root.matrixWorld).invert().multiply(o.matrixWorld));
      boxUV(o.geometry, local, texSize(key));
    }
    o.castShadow = key !== 'glass' && key !== 'light_diffuser';
    o.receiveShadow = true;
  });
  // Downlights belong to the (cut-away) ceiling: shadow casters only, still seen by the probe.
  if (it.code === DOWNLIGHT) {
    root.traverse(o => o.layers.set(LAYER_GHOST));
    if (it.center) centerFixture = root; else spotFixtures.push(root);
  }
  products.add(root);
  (roots[it.code] ??= []).push(root);
}));

// ---------------------------------------------------------------------------
// Lights
// ---------------------------------------------------------------------------
// Single ceiling light in the centre. Just below the fixture: a light inside its own housing
// overflows the probe (inverse-square).
const ceilLight = new THREE.PointLight(CEIL_COLOR, 5, 0, 2);
ceilLight.position.set(RW / 2, RH - 0.03, RD / 2);
ceilLight.castShadow = true;
ceilLight.shadow.mapSize.set(1024, 1024);
ceilLight.shadow.bias = -0.0003;
ceilLight.shadow.normalBias = 0.015;
ceilLight.shadow.radius = 4;
ceilLight.shadow.camera.near = 0.05;
ceilLight.shadow.camera.layers.enable(LAYER_GHOST);
scene.add(ceilLight);

// The design's four ceiling spots, off by default.
const spots = [];
for (const p of scene3.downlights) {
  const s = new THREE.SpotLight(CEIL_COLOR, 5, 0, THREE.MathUtils.degToRad(58), 1, 2);
  s.position.set(p[0], p[1] - 0.03, p[2]);
  s.target.position.set(p[0], 0, p[2]);
  s.castShadow = true;
  s.shadow.mapSize.set(1024, 1024);
  s.shadow.bias = -0.0003;
  s.shadow.normalBias = 0.015;
  s.shadow.radius = 4;
  s.shadow.camera.near = 0.05;
  s.shadow.camera.layers.enable(LAYER_GHOST);
  scene.add(s, s.target);
  spots.push(s);
}
function setSpots(on) {
  spots.forEach(l => (l.visible = on));
  spotFixtures.forEach(f => (f.visible = on)); // hides their glowing diffusers in the probe too
}
setSpots(false);

// Soft LED strip under the mirror cabinet. Cabinet local frame: x along its width, y up from the
// underside, z from the wall (0) to the front (-0.14).
const ledLights = [];
for (const root of roots[MIRROR_CABINET] ?? []) {
  const z = -0.14 + LED.setback;
  const strip = new THREE.Mesh(new THREE.BoxGeometry(LED.length, 0.003, LED.width), M.led_strip);
  strip.position.set(0, -0.0015, z);
  root.add(strip);
  // Diffused output: a slightly wider emitter than the strip itself keeps the wash soft.
  const area = new THREE.RectAreaLight(LED.color, LED.intensity, LED.length, LED.width * 3);
  area.position.set(0, -0.004, z);
  area.rotation.x = -Math.PI / 2; // emit downwards
  root.add(area);
  ledLights.push(area);
}
// Very weak fill so nothing is pitch black before the environment probe is built.
const fill = new THREE.HemisphereLight(0xffffff, 0xd8d2c8, 0.15);
scene.add(fill);

// Environment probe captured from inside the room: gives bounce light and real reflections
// (mirror, gunmetal) of the actual tiles. Two passes ~ two light bounces.
const pmrem = new THREE.PMREMGenerator(renderer);
let envTex = null;
function captureEnvironment() {
  const rt = new THREE.WebGLCubeRenderTarget(256, { type: THREE.HalfFloatType });
  const cubeCam = new THREE.CubeCamera(0.02, 30, rt);
  cubeCam.position.set(RW / 2, 1.35, RD / 2);
  cubeCam.children.forEach(c => c.layers.enableAll());
  scene.add(cubeCam);
  // Transmissive materials trigger three's transmission pass, which restores the render target
  // without its cube face: every face would end up in face 0. Hide them for the capture.
  const hidden = [];
  scene.traverse(o => { if (o.isMesh && o.visible && o.material.transmission > 0) { o.visible = false; hidden.push(o); } });
  const prevTM = renderer.toneMapping;
  renderer.toneMapping = THREE.NoToneMapping;
  for (let pass = 0; pass < 2; pass++) {
    cubeCam.update(renderer, scene);
    const next = pmrem.fromCubemap(rt.texture).texture;
    envTex?.dispose();
    envTex = next;
    scene.environment = envTex;
    scene.environmentIntensity = ENV_INTENSITY;
  }
  renderer.toneMapping = prevTM;
  hidden.forEach(o => (o.visible = true));
  scene.remove(cubeCam);
  rt.dispose();
}

// ---------------------------------------------------------------------------
// Isometric view + cut-away of the walls facing the camera
// ---------------------------------------------------------------------------
let quadrant = 0; // 0..3, which room corner the camera looks from
let viewAngle = 0, targetAngle = 0;
const ISO_ELEV = Math.atan(1 / Math.SQRT2); // 35.26 degrees: true isometric
const baseAngle = -3 * Math.PI / 4;          // start: from the window/toilet corner (x=0, z=0)

function viewDir(angle) {
  return new THREE.Vector3(Math.cos(angle) * Math.cos(ISO_ELEV), Math.sin(ISO_ELEV), Math.sin(angle) * Math.cos(ISO_ELEV));
}

function fitCamera() {
  const dir = viewDir(baseAngle + viewAngle);
  camera.position.copy(controls.target).addScaledVector(dir, 20);
  camera.lookAt(controls.target);
  camera.updateMatrixWorld();
  // fit the room box in view
  const inv = camera.matrixWorldInverse;
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const x of [-WALL_T, RW + WALL_T]) for (const y of [0, RH]) for (const z of [-WALL_T, RD + WALL_T]) {
    const v = new THREE.Vector3(x, y, z).applyMatrix4(inv);
    minX = Math.min(minX, v.x); maxX = Math.max(maxX, v.x); minY = Math.min(minY, v.y); maxY = Math.max(maxY, v.y);
  }
  const aspect = innerWidth / innerHeight;
  const halfH = Math.max((maxY - minY) / 2, (maxX - minX) / 2 / aspect) * 1.08;
  const cy = (minY + maxY) / 2, cx = (minX + maxX) / 2;
  camera.left = cx - halfH * aspect; camera.right = cx + halfH * aspect;
  camera.top = cy + halfH; camera.bottom = cy - halfH;
  camera.updateProjectionMatrix();
}

function updateCutaway() {
  const toCam = viewDir(baseAngle + viewAngle);
  for (const w of Object.values(walls)) {
    // A wall whose inner face points at the camera hides the room: draw it as shadow-caster only.
    const near = w.normal.dot(toCam) < -0.05;
    w.mesh.layers.set(near ? LAYER_GHOST : 0);
    w.extras.forEach(e => e.layers.set(near ? LAYER_GHOST : 0));
  }
}

function rotate(step) {
  quadrant = (quadrant + step + 4) % 4;
  targetAngle += step * Math.PI / 2;
  markDirty();
}

// ---------------------------------------------------------------------------
// Post-processing
// ---------------------------------------------------------------------------
const composerRT = new THREE.WebGLRenderTarget(innerWidth, innerHeight, { type: THREE.HalfFloatType, samples: 4 });
const composer = new EffectComposer(renderer, composerRT);
composer.setPixelRatio(renderer.getPixelRatio());
composer.addPass(new RenderPass(scene, camera));
const gtao = new GTAOPass(scene, camera, innerWidth, innerHeight);
gtao.updateGtaoMaterial({ radius: 0.22, distanceExponent: 1.4, thickness: 1.2, scale: 1.0, samples: 16 });
gtao.updatePdMaterial({ lumaPhi: 10, depthPhi: 2, normalPhi: 3, radius: 6, rings: 2, samples: 16 });
gtao.blendIntensity = 0.85;
composer.addPass(gtao);
composer.addPass(new OutputPass());

// ---------------------------------------------------------------------------
// UI
// ---------------------------------------------------------------------------
function toggle(btn, fn) {
  btn.addEventListener('click', () => {
    const on = btn.getAttribute('aria-pressed') !== 'true';
    btn.setAttribute('aria-pressed', String(on));
    fn(on);
  });
}
document.getElementById('rotL').onclick = () => rotate(-1);
document.getElementById('rotR').onclick = () => rotate(1);
addEventListener('keydown', e => { if (e.key === 'q') rotate(-1); if (e.key === 'e') rotate(1); });
toggle(document.getElementById('day'), on => {
  windowLights.forEach(l => (l.visible = on)); M.sky.color.set(on ? DAY_COLOR : 0x30343a).multiplyScalar(on ? 3 : 1);
  relight();
});
toggle(document.getElementById('ceil'), on => {
  ceilLight.visible = on; centerFixture.visible = on; relight();
});
toggle(document.getElementById('spots'), on => { setSpots(on); relight(); });
toggle(document.getElementById('led'), on => {
  ledLights.forEach(l => (l.visible = on)); M.led_strip.emissiveIntensity = on ? 5 : 0; relight();
});
document.getElementById('exposure').oninput = e => { renderer.toneMappingExposure = +e.target.value; markDirty(); };
controls.addEventListener('change', () => markDirty());
addEventListener('resize', () => {
  renderer.setSize(innerWidth, innerHeight);
  composer.setSize(innerWidth, innerHeight);
  gtao.setSize(innerWidth, innerHeight);
  fitCamera();
  markDirty();
});

function relight() {
  // re-capture with all walls present
  const masks = Object.values(walls).map(w => w.mesh.layers.mask);
  Object.values(walls).forEach(w => w.mesh.layers.set(0));
  ceiling.layers.set(0);
  captureEnvironment();
  Object.values(walls).forEach((w, i) => (w.mesh.layers.mask = masks[i]));
  ceiling.layers.set(LAYER_GHOST);
  markDirty();
}

// ---------------------------------------------------------------------------
// Go
// ---------------------------------------------------------------------------
fitCamera();
updateCutaway();
await new Promise(r => setTimeout(r, 300)); // let textures arrive before probing
relight();
window.__ready = true;

renderer.setAnimationLoop(() => {
  const d = targetAngle - viewAngle;
  if (Math.abs(d) > 1e-4) {
    viewAngle += d * 0.14;
    if (Math.abs(targetAngle - viewAngle) < 1e-3) viewAngle = targetAngle;
    fitCamera();
    updateCutaway();
    markDirty();
  }
  controls.update();
  if (!dirty && warmup <= 0) return;
  dirty = false;
  warmup--;
  if (warmup === 0) status.textContent = "";
  composer.render();
});
window.__three = { scene, camera, renderer, walls, rotate };
