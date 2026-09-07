import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';

const MODEL_URL = '../models/Robot3D.glb';

const TARGET_H = 2.0;
const FIT_MARGIN = 1.28;

// Eyeballed with ?r3d=1; the GLB has no orientation convention.
const MODEL_YAW_OFFSET = 2.094;
const MODEL_NUDGE_X = 0;
const MODEL_NUDGE_Y = 0;

const YAW_MAX = 0.50;
const PITCH_MAX = 0.25;
const LERP = 0.085;
const IDLE_MS = 2200;

const G = '#34d058';

// The GLB ships flat red/cyan materials from obj2gltf. The gray ramp sits well
// above --bg (#0d1117) so the body reads as a silhouette instead of a hole.
const MATERIAL_OVERRIDES = {
  mat23: { color: '#252c35', metalness: 0.60, roughness: 0.38 },
  mat17: { color: '#333b45', metalness: 0.50, roughness: 0.42 },
  mat16: { color: '#454e59', metalness: 0.55, roughness: 0.38 },
  mat22: { color: '#5f6874', metalness: 0.70, roughness: 0.28 },
  mat15: { color: '#9fa9b4', metalness: 0.80, roughness: 0.22 },
  mat25: {
    color: '#cfe9d6', metalness: 0.0, roughness: 0.12,
    transparent: true, opacity: 0.35, side: THREE.DoubleSide, depthWrite: false,
  },
  mat3: { color: G, emissive: G, emissiveIntensity: 1.20, metalness: 0.0, roughness: 0.40 },
  mat14: { color: G, emissive: G, emissiveIntensity: 1.50, metalness: 0.0, roughness: 0.35 },
  mat8: { color: '#1b3a1b', emissive: G, emissiveIntensity: 0.35, metalness: 0.30, roughness: 0.55 },
};

const GRAY_RAMP = ['#252c35', '#333b45', '#454e59', '#5f6874', '#9fa9b4', '#e6edf3'];

// Fallback for materials missing from the table. The luminance floor keeps a
// dark but saturated blue-gray (the shell) from being read as an accent.
function classify(material) {
  const c = material.color;
  const lum = 0.2126 * c.r + 0.7152 * c.g + 0.0722 * c.b;
  const max = Math.max(c.r, c.g, c.b);
  const min = Math.min(c.r, c.g, c.b);
  const sat = max > 0 ? (max - min) / max : 0;
  return { accent: sat > 0.5 && lum > 0.15, lum };
}

function assign(m, spec) {
  if (spec.color) m.color.set(spec.color);
  if (spec.emissive) m.emissive.set(spec.emissive);
  if (spec.emissiveIntensity != null) m.emissiveIntensity = spec.emissiveIntensity;
  if (spec.metalness != null) m.metalness = spec.metalness;
  if (spec.roughness != null) m.roughness = spec.roughness;
  if (spec.side) m.side = spec.side;
  if (spec.transparent) {
    m.transparent = true;
    m.opacity = spec.opacity;
    m.depthWrite = spec.depthWrite !== false;
  }
}

function applyMaterials(model) {
  const seen = new Set();
  const unnamed = [];

  model.traverse((o) => {
    if (!o.isMesh) return;

    o.castShadow = false;
    o.receiveShadow = false;
    o.geometry.deleteAttribute('uv'); // model has no textures

    const materials = Array.isArray(o.material) ? o.material : [o.material];
    for (const m of materials) {
      if (seen.has(m.uuid)) continue;
      seen.add(m.uuid);

      const spec = MATERIAL_OVERRIDES[m.name];
      if (spec) assign(m, spec);
      else unnamed.push(m);

      m.envMapIntensity = 0.9;
      m.needsUpdate = true;
    }
  });

  if (!unnamed.length) return;

  const grays = [];
  for (const m of unnamed) {
    const info = classify(m);
    if (info.accent) {
      assign(m, { color: G, emissive: G, emissiveIntensity: 1.2, metalness: 0.0, roughness: 0.4 });
    } else {
      grays.push({ material: m, lum: info.lum });
    }
  }

  grays.sort((a, b) => a.lum - b.lum);
  grays.forEach((entry, i) => {
    const step = grays.length > 1
      ? Math.round((i / (grays.length - 1)) * (GRAY_RAMP.length - 1))
      : 0;
    assign(entry.material, { color: GRAY_RAMP[step], metalness: 0.55, roughness: 0.45 });
  });
}

function buildLights(scene) {
  scene.add(new THREE.AmbientLight(0xffffff, 0.35));

  const key = new THREE.DirectionalLight(0xffffff, 2.6);
  key.position.set(2.5, 3.5, 4.0);
  scene.add(key);

  const fill = new THREE.DirectionalLight(0x8b949e, 0.9);
  fill.position.set(-4.0, 0.5, 2.5);
  scene.add(fill);

  // Two back lights outline the silhouette against the dark page.
  const rimGreen = new THREE.DirectionalLight(0x34d058, 4.2);
  rimGreen.position.set(-2.0, 1.8, -4.0);
  scene.add(rimGreen);

  const rimCool = new THREE.DirectionalLight(0xc8d6e5, 2.2);
  rimCool.position.set(3.2, 1.2, -3.5);
  scene.add(rimCool);

  const glow = new THREE.PointLight(0x34d058, 3.0, 6, 2);
  glow.position.set(0.9, -0.4, 1.6);
  scene.add(glow);
}

// The four nozzles are instances of the same 312-vertex piece, shipped in the
// two accent materials and sitting in the bottom quarter of the model.
const NOZZLE_MATERIALS = new Set(['mat14', 'mat3']);
const FLAME_LENGTH = 0.62;  // model units, before root scale
const FLAME_SPREAD = 1.35;  // outer cone radius vs the nozzle's own
const SPARKS_PER_NOZZLE = 40;

const NOISE_GLSL = `
  float hash(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
  float noise(vec2 p) {
    vec2 i = floor(p), f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    return mix(mix(hash(i), hash(i + vec2(1, 0)), f.x),
               mix(hash(i + vec2(0, 1)), hash(i + vec2(1, 1)), f.x), f.y);
  }
`;

// Premultiplied additive: One/One, with the shader outputting rgb * alpha. The
// default SrcAlpha factor would multiply by alpha twice.
function additiveMaterial(uniforms, vertexShader, fragmentShader) {
  return new THREE.ShaderMaterial({
    uniforms,
    vertexShader,
    fragmentShader,
    transparent: true,
    depthWrite: false,
    blending: THREE.CustomBlending,
    blendEquation: THREE.AddEquation,
    blendSrc: THREE.OneFactor,
    blendDst: THREE.OneFactor,
  });
}

function makePlume(radius, height, core, edge, gain, seed) {
  const geometry = new THREE.ConeGeometry(radius, height, 14, 1, true);
  geometry.rotateX(Math.PI);          // taper downward
  geometry.translate(0, -height / 2, 0); // mouth at the local origin

  const material = additiveMaterial(
    {
      uTime: { value: 0 },
      uHeight: { value: height },
      uSeed: { value: seed },
      uCore: { value: new THREE.Color(core) },
      uEdge: { value: new THREE.Color(edge) },
      uGain: { value: gain },
    },
    `
      uniform float uHeight;
      varying float vT;
      varying vec2 vUv;
      void main() {
        vT = clamp(0.5 - position.y / uHeight, 0.0, 1.0);
        vUv = uv;
        gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
      }
    `,
    NOISE_GLSL + `
      uniform float uTime;
      uniform float uSeed;
      uniform vec3 uCore;
      uniform vec3 uEdge;
      uniform float uGain;
      varying float vT;
      varying vec2 vUv;
      void main() {
        float turb = noise(vec2(vUv.x * 6.0, vT * 3.0 - uTime * 2.4 + uSeed));
        float fade = pow(1.0 - vT, 1.5);
        float a = fade * (0.55 + 0.45 * turb) * uGain;
        a *= 0.85 + 0.15 * sin(uTime * 29.0 + uSeed * 6.0);
        if (a <= 0.001) discard;
        vec3 col = mix(uEdge, uCore, fade);
        gl_FragColor = vec4(col * a, a);
      }
    `,
  );
  material.side = THREE.DoubleSide;

  return new THREE.Mesh(geometry, material);
}

function makeSparks(radius, color, pixelRatio) {
  const positions = new Float32Array(SPARKS_PER_NOZZLE * 3);
  const seeds = new Float32Array(SPARKS_PER_NOZZLE);
  for (let i = 0; i < SPARKS_PER_NOZZLE; i++) {
    const a = Math.random() * Math.PI * 2;
    const r = Math.sqrt(Math.random()) * radius * 0.8;
    positions[i * 3] = Math.cos(a) * r;
    positions[i * 3 + 2] = Math.sin(a) * r;
    seeds[i] = Math.random();
  }

  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
  geometry.setAttribute('aSeed', new THREE.BufferAttribute(seeds, 1));

  const material = additiveMaterial(
    {
      uTime: { value: 0 },
      uLength: { value: FLAME_LENGTH * 1.5 },
      uSize: { value: 0.22 * pixelRatio },
      uColor: { value: new THREE.Color(color) },
    },
    `
      attribute float aSeed;
      uniform float uTime;
      uniform float uLength;
      uniform float uSize;
      varying float vLife;
      void main() {
        vLife = fract(uTime * (0.6 + aSeed * 0.5) + aSeed);
        vec3 p = position;
        p.xz *= 1.0 + vLife * 1.8;
        p.y -= vLife * uLength;
        vec4 mv = modelViewMatrix * vec4(p, 1.0);
        gl_PointSize = max(1.0, uSize * (1.0 - vLife * 0.6) * (300.0 / -mv.z));
        gl_Position = projectionMatrix * mv;
      }
    `,
    `
      uniform vec3 uColor;
      varying float vLife;
      void main() {
        float d = length(gl_PointCoord - 0.5) * 2.0;
        float a = smoothstep(1.0, 0.0, d) * (1.0 - vLife);
        if (a <= 0.001) discard;
        gl_FragColor = vec4(uColor * a, a);
      }
    `,
  );

  return new THREE.Points(geometry, material);
}

/**
 * Finds the nozzles and hangs a flame off each. Returns null when the model
 * carries none, so a re-export without them degrades to no thrusters.
 */
function buildThrusters(model, modelBox, pixelRatio) {
  model.updateMatrixWorld(true);
  const cut = modelBox.min.y + (modelBox.max.y - modelBox.min.y) * 0.25;

  const nozzles = [];
  model.traverse((o) => {
    if (!o.isMesh || Array.isArray(o.material)) return;
    if (!NOZZLE_MATERIALS.has(o.material.name)) return;

    o.geometry.computeBoundingBox();
    const box = o.geometry.boundingBox.clone().applyMatrix4(o.matrixWorld);
    const center = box.getCenter(new THREE.Vector3());
    if (center.y > cut) return;

    nozzles.push({
      x: center.x,
      y: box.min.y,
      z: center.z,
      r: Math.max(box.max.x - box.min.x, box.max.z - box.min.z) * 0.5,
    });
  });

  if (!nozzles.length) return null;

  const materials = [];
  const center = new THREE.Vector3();

  for (const n of nozzles) {
    const holder = new THREE.Group();
    holder.position.set(n.x, n.y, n.z);
    const seed = Math.random() * 10;

    // Nested cones: the additive overlap is what gives the hot core.
    const outer = makePlume(n.r * FLAME_SPREAD, FLAME_LENGTH, '#8bf5a3', G, 0.55, seed);
    const inner = makePlume(n.r * 0.7, FLAME_LENGTH * 0.68, '#eafff0', '#8bf5a3', 0.85, seed + 3.1);
    const sparks = makeSparks(n.r, G, pixelRatio);

    holder.add(outer, inner, sparks);
    model.add(holder);

    materials.push(outer.material, inner.material, sparks.material);
    center.add(holder.position);
  }

  center.divideScalar(nozzles.length);

  // One shared bounce light instead of one per nozzle: each extra light
  // re-runs the lighting loop over all 69 primitives.
  const bounce = new THREE.PointLight(0x34d058, 0, 2.4, 2);
  bounce.position.set(center.x, center.y - FLAME_LENGTH * 0.35, center.z);
  model.add(bounce);

  return {
    update(t) {
      for (const m of materials) m.uniforms.uTime.value = t;
      bounce.intensity = 2.6 + 0.9 * Math.sin(t * 17.0) * Math.sin(t * 11.3);
    },
  };
}

function startCalibration({ scene, root, pivot, camera, box, scale }) {
  scene.add(new THREE.AxesHelper(2));
  scene.add(new THREE.Box3Helper(new THREE.Box3().setFromObject(root), 0x34d058));

  const hud = document.createElement('pre');
  hud.style.cssText = 'position:fixed;left:12px;bottom:12px;z-index:99;margin:0;'
    + 'padding:10px 14px;background:#161b22;border:1px solid #34d058;border-radius:8px;'
    + 'color:#e6edf3;font:12px/1.5 monospace;white-space:pre;pointer-events:none';
  document.body.appendChild(hud);

  function refresh() {
    hud.textContent = [
      'arrows: rotate   shift+arrows: move',
      '',
      'MODEL_YAW_OFFSET = ' + pivot.rotation.y.toFixed(3),
      'MODEL_NUDGE_X    = ' + pivot.position.x.toFixed(3),
      'MODEL_NUDGE_Y    = ' + pivot.position.y.toFixed(3),
    ].join('\n');
  }

  window.addEventListener('keydown', (ev) => {
    const step = ev.shiftKey ? 0.02 : Math.PI / 60;
    if (ev.key === 'ArrowLeft') {
      if (ev.shiftKey) pivot.position.x -= step; else pivot.rotation.y -= step;
    } else if (ev.key === 'ArrowRight') {
      if (ev.shiftKey) pivot.position.x += step; else pivot.rotation.y += step;
    } else if (ev.key === 'ArrowUp') {
      pivot.position.y += 0.02;
    } else if (ev.key === 'ArrowDown') {
      pivot.position.y -= 0.02;
    } else {
      return;
    }
    ev.preventDefault();
    refresh();
  });

  refresh();
  console.log('[robot3d] bbox', box.min, box.max, 'scale', scale, 'camDist', camera.position.z);
}

export function initRobot3D(host) {
  const canvas = host.querySelector('.hero-3d__canvas');
  if (!canvas) throw new Error('missing .hero-3d__canvas');

  const debug = new URLSearchParams(location.search).has('r3d');

  const renderer = new THREE.WebGLRenderer({
    canvas,
    alpha: true,
    antialias: true,
    powerPreference: 'high-performance',
  });
  renderer.setClearAlpha(0); // let --bg and the body::before halo show through
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, host.clientWidth < 600 ? 1.5 : 2));
  renderer.toneMapping = THREE.NeutralToneMapping;
  renderer.toneMappingExposure = 1.05;

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(32, 1, 0.1, 100);

  buildLights(scene);

  // Without an environment, anything metallic renders near black.
  const pmrem = new THREE.PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
  scene.environmentIntensity = 0.55;
  pmrem.dispose();

  const root = new THREE.Group();     // driven by the cursor, starts at (0,0,0)
  const pivot = new THREE.Object3D(); // holds the GLB calibration
  pivot.rotation.y = MODEL_YAW_OFFSET;
  pivot.position.set(MODEL_NUDGE_X, MODEL_NUDGE_Y, 0);
  root.add(pivot);
  scene.add(root);

  const normSize = new THREE.Vector3(1, TARGET_H, 1);

  function updateCamera() {
    const w = host.clientWidth;
    const h = host.clientHeight;
    if (!w || !h) return;

    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h, false); // CSS owns the element size

    const vFov = THREE.MathUtils.degToRad(camera.fov);
    const hFov = 2 * Math.atan(Math.tan(vFov / 2) * camera.aspect);
    const distH = (normSize.y / 2) / Math.tan(vFov / 2);
    // Widest of X/Z: fitting X alone lets the arms clip once it yaws.
    const distW = (Math.max(normSize.x, normSize.z) / 2) / Math.tan(hFov / 2);
    const dist = Math.max(distH, distW) * FIT_MARGIN;

    camera.position.set(0, 0, dist);
    camera.lookAt(0, 0, 0);
  }

  const target = { yaw: 0, pitch: 0 };
  const current = { yaw: 0, pitch: 0 };
  let lastMove = 0;
  let thrusters = null;

  // On touch, pointermove only fires while dragging, which would leave the
  // robot frozen mid-turn; it sways on its own instead.
  const finePointer = window.matchMedia('(hover: hover) and (pointer: fine)').matches;

  function recenter() {
    target.yaw = 0;
    target.pitch = 0;
  }

  function onPointerMove(e) {
    // Normalized against the canvas center, not the window's.
    const r = host.getBoundingClientRect();
    const nx = THREE.MathUtils.clamp(
      (e.clientX - (r.left + r.width / 2)) / (window.innerWidth / 2), -1, 1);
    const ny = THREE.MathUtils.clamp(
      (e.clientY - (r.top + r.height / 2)) / (window.innerHeight / 2), -1, 1);
    target.yaw = nx * YAW_MAX;
    target.pitch = ny * PITCH_MAX;
    lastMove = performance.now();
  }

  if (finePointer) {
    window.addEventListener('pointermove', onPointerMove, { passive: true });
    window.addEventListener('pointerleave', recenter);
    document.addEventListener('mouseleave', recenter);
  }

  let running = false;

  function tick(t) {
    if (!finePointer) {
      target.yaw = Math.sin(t * 0.00042) * YAW_MAX * 0.55;
      target.pitch = Math.sin(t * 0.00031) * PITCH_MAX * 0.40;
    } else if (t - lastMove > IDLE_MS) {
      target.yaw *= 0.94;
      target.pitch *= 0.94;
    }

    current.yaw += (target.yaw - current.yaw) * LERP;
    current.pitch += (target.pitch - current.pitch) * LERP;

    root.rotation.y = current.yaw;
    root.rotation.x = current.pitch;
    root.position.y = Math.sin(t * 0.0011) * 0.035;

    if (thrusters) thrusters.update(t * 0.001);

    renderer.render(scene, camera);
  }

  function play() {
    if (running) return;
    running = true;
    renderer.setAnimationLoop(tick);
  }

  function pause() {
    if (!running) return;
    running = false;
    renderer.setAnimationLoop(null);
  }

  let visible = false;
  const ro = new ResizeObserver(updateCamera);
  ro.observe(host);

  new IntersectionObserver(([entry]) => {
    visible = entry.isIntersecting;
    if (visible) play();
    else pause();
  }, { threshold: 0.01 }).observe(host);

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) pause();
    else if (visible) play();
  });

  canvas.addEventListener('webglcontextlost', (e) => { e.preventDefault(); pause(); });
  canvas.addEventListener('webglcontextrestored', play);

  const reduceMq = window.matchMedia('(prefers-reduced-motion: reduce)');
  reduceMq.addEventListener('change', (e) => {
    if (!e.matches) {
      if (visible) play();
      return;
    }
    recenter();
    current.yaw = 0;
    current.pitch = 0;
    root.rotation.set(0, 0, 0);
    root.position.y = 0;
    renderer.render(scene, camera);
    pause();
  });

  return new Promise((resolve, reject) => {
    new GLTFLoader().load(
      MODEL_URL,
      (gltf) => {
        const model = gltf.scene;
        applyMaterials(model);

        // Measured before the flames exist, so the exhaust never inflates the
        // bounds used for framing.
        const box = new THREE.Box3().setFromObject(model);
        const size = box.getSize(new THREE.Vector3());
        const center = box.getCenter(new THREE.Vector3());

        // Built while the model still sits at identity: the flames become
        // children and inherit the recentering below.
        thrusters = buildThrusters(model, box, renderer.getPixelRatio());
        // Only part of the plume is reserved; its tip fades to nothing, so
        // clipping the last stretch is invisible and keeps the robot larger.
        const flameDrop = thrusters ? FLAME_LENGTH * 0.8 : 0;

        // The GLB center is off origin: without this it spins around a pivot
        // outside its own body.
        model.position.sub(center);
        model.position.y += flameDrop / 2;
        pivot.add(model);

        const scale = TARGET_H / (size.y + flameDrop);
        root.scale.setScalar(scale);
        normSize.set(size.x, size.y + flameDrop, size.z).multiplyScalar(scale);

        updateCamera();
        host.classList.add('is-ready');
        if (visible) play();

        if (debug) startCalibration({ scene, root, pivot, camera, box, scale });

        resolve();
      },
      (ev) => {
        if (ev.lengthComputable) {
          host.style.setProperty('--p', (ev.loaded / ev.total).toFixed(3));
        }
      },
      reject,
    );
  });
}
