/* Hunter console — the approved mock-up (v12) as a renderer of genius-ai's live data.
   Data: the bridge (console/server.py) through app.js's store; actions through app.js's api.
     sectors  = the first four projects by rank: alive / wounded (a problem) / on the hunt
                (release window open or an operation in work) / dormant (paused)
     spawns   = live agents and sub-agents of those projects, with their real states and names
     marks    = problems and operations that wait for the user, with their real text
     dots     = an operation's own answers (1–3); L = «later»
     core     = the locked or top waiting item, else the focus of the day
     channel  = a line typed here goes to the coordinator's terminal (hc send); the reply shown is
                the coordinator's latest final reply (state/log, written by the Stop hook)
   Two draw calls: (1) one full-screen thermal field shader: hex sensor mosaic, four sectors,
   heat signatures, core, reticle; (2) one instanced glyph layer from an atlas drawn in code
   (48 own segmented glyphs + Latin and Cyrillic for decoded lines).
   Rest: ~12 fps. Transition: display rate. Hidden tab or the other skin: stopped. */
import * as THREE from 'three';

const REST_FPS = 12;
const TAU = Math.PI * 2;
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const lerp = (a, b, t) => a + (b - a) * t;
const ease = (cur, target, dt, k) => cur + (target - cur) * (1 - Math.exp(-dt * k));
const h1 = (n) => { const s = Math.sin(n * 127.1 + 311.7) * 43758.5453; return s - Math.floor(s); };

/* ---------- renderer ---------- */
const canvas = document.getElementById('field');
const renderer = new THREE.WebGLRenderer({ canvas, antialias: false, alpha: false, powerPreference: 'low-power', preserveDrawingBuffer: true });
renderer.setClearColor(0x000000, 1);
const scene = new THREE.Scene();
const camera = new THREE.Camera();
let W = 1, H = 1, DPR = 1;
const L = {}; // layout

/* ---------- glyph atlas (drawn in code) ---------- */
// Readable characters: ASCII, a few marks, Ukrainian and Russian Cyrillic. Then one solid block.
const TEXT_CHARS = (() => { let s = ''; for (let c = 32; c < 127; c++) s += String.fromCharCode(c);
  return s + '·«»—–…’“”№●◆▸◂‹›←→↑↓⌥⌫' + 'АБВГҐДЕЄЁЖЗИІЇЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ' + 'абвгґдеєёжзиіїйклмнопрстуфхцчшщъыьэюя'; })();
const N_ALIEN = 48, COLS = 16, CW = 56, CH = 96, PAD = 4;
const SOLID = N_ALIEN + TEXT_CHARS.length;
const ROWS = Math.ceil((SOLID + 1) / COLS);
const PITCH_X = CW + PAD * 2, PITCH_Y = CH + PAD * 2;
const ATLAS_W = COLS * PITCH_X, ATLAS_H = ROWS * PITCH_Y;

function buildAtlas() {
  const cv = document.createElement('canvas');
  cv.width = ATLAS_W; cv.height = ATLAS_H;
  const g = cv.getContext('2d');
  g.fillStyle = '#000'; g.fillRect(0, 0, ATLAS_W, ATLAS_H);
  g.fillStyle = '#fff';

  // Own alphabet: a hexagonal cell with six rim segments, six spokes and four marks.
  const P = [[28, 10], [8, 29], [48, 29], [28, 48], [8, 67], [48, 67], [28, 86]];
  const SEG = [[0, 1], [0, 2], [1, 4], [2, 5], [4, 6], [5, 6], [0, 3], [3, 6], [1, 3], [2, 3], [4, 3], [5, 3]];
  const bar = (ox, oy, a, b, wd) => {
    const dx = b[0] - a[0], dy = b[1] - a[1], len = Math.hypot(dx, dy), ux = dx / len, uy = dy / len;
    const nx = -uy * wd / 2, ny = ux * wd / 2, gap = 3.2, tip = wd / 2;
    const s0 = gap, s1 = len - gap;
    const pt = (s, k) => [ox + a[0] + ux * s + nx * k, oy + a[1] + uy * s + ny * k];
    const pts = [pt(s0, 0), pt(s0 + tip, 1), pt(s1 - tip, 1), pt(s1, 0), pt(s1 - tip, -1), pt(s0 + tip, -1)];
    g.beginPath(); pts.forEach((p, i) => (i ? g.lineTo(p[0], p[1]) : g.moveTo(p[0], p[1]))); g.closePath(); g.fill();
  };
  const diamond = (x, y, r) => { g.beginPath(); g.moveTo(x, y - r); g.lineTo(x + r, y); g.lineTo(x, y + r); g.lineTo(x - r, y); g.closePath(); g.fill(); };

  let seed = 20261118;
  const rnd = () => { seed |= 0; seed = seed + 0x6D2B79F5 | 0; let t = Math.imul(seed ^ seed >>> 15, 1 | seed); t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; };
  const seen = new Set();
  for (let i = 0; i < N_ALIEN; i++) {
    let mask = 0;
    do {
      mask = 0;
      const n = 4 + Math.floor(rnd() * 4);
      while (bitCount(mask & 0xfff) < n) mask |= 1 << Math.floor(rnd() * 12);
      if (rnd() < 0.45) mask |= 1 << (12 + Math.floor(rnd() * 4));
    } while (seen.has(mask));
    seen.add(mask);
    const ox = (i % COLS) * PITCH_X + PAD, oy = Math.floor(i / COLS) * PITCH_Y + PAD;
    for (let s = 0; s < 12; s++) if (mask >> s & 1) bar(ox, oy, P[SEG[s][0]], P[SEG[s][1]], 11.5);
    if (mask >> 12 & 1 && !(mask & 0xfc0)) diamond(ox + 28, oy + 48, 9);
    if (mask >> 13 & 1) bar(ox, oy, [32, 5], [56, 5], 9);
    if (mask >> 14 & 1) bar(ox, oy, [0, 91], [24, 91], 9);
    if (mask >> 15 & 1) { diamond(ox + 28, oy + 29, 5); diamond(ox + 28, oy + 67, 5); }
  }
  // Readable text, only for decoded lines and the command line.
  g.font = '700 80px ui-monospace, "SF Mono", Menlo, Consolas, monospace';
  g.textAlign = 'center'; g.textBaseline = 'alphabetic';
  for (let k = 0; k < TEXT_CHARS.length; k++) {
    const i = N_ALIEN + k, chr = TEXT_CHARS[k];
    const ox = (i % COLS) * PITCH_X + PAD, oy = Math.floor(i / COLS) * PITCH_Y + PAD;
    const w = g.measureText(chr).width;
    if (w > CW - 4) { g.save(); g.translate(ox + CW / 2, 0); g.scale((CW - 4) / w, 1); g.fillText(chr, 0, oy + 76); g.restore(); }
    else g.fillText(chr, ox + CW / 2, oy + 76);
  }
  { const ox = (SOLID % COLS) * PITCH_X + PAD, oy = Math.floor(SOLID / COLS) * PITCH_Y + PAD; g.fillRect(ox, oy, CW, CH); }
  const tex = new THREE.CanvasTexture(cv);
  tex.generateMipmaps = true;
  tex.minFilter = THREE.LinearMipmapLinearFilter;
  tex.magFilter = THREE.LinearFilter;
  return tex;
}
function bitCount(n) { let c = 0; while (n) { c += n & 1; n >>>= 1; } return c; }
const CHAR_IDX = new Map();
for (let k = 0; k < TEXT_CHARS.length; k++) CHAR_IDX.set(TEXT_CHARS[k], N_ALIEN + k);
const latinIdx = (ch) => CHAR_IDX.get(ch) ?? CHAR_IDX.get('?');
const alienAt = (i, tick) => Math.floor(h1(i * 13.37 + tick * 7.913) * N_ALIEN);

/* ---------- glyph layer: one instanced draw call ---------- */
const MAX_GLYPHS = 2400;
const gRect = new Float32Array(MAX_GLYPHS * 4);
const gCol = new Float32Array(MAX_GLYPHS * 4);
const gIdx = new Float32Array(MAX_GLYPHS);
let gN = 0;
function glyph(x, y, w, h, idx, r, g, b, a) {
  if (gN >= MAX_GLYPHS || a <= 0.004) return;
  const o = gN * 4;
  gRect[o] = x; gRect[o + 1] = y; gRect[o + 2] = w; gRect[o + 3] = h;
  gCol[o] = r; gCol[o + 1] = g; gCol[o + 2] = b; gCol[o + 3] = a;
  gIdx[gN++] = idx;
}
const glyphGeo = new THREE.InstancedBufferGeometry();
{
  const base = new THREE.PlaneGeometry(1, 1);
  glyphGeo.index = base.index;
  glyphGeo.setAttribute('position', base.getAttribute('position'));
  glyphGeo.setAttribute('aRect', new THREE.InstancedBufferAttribute(gRect, 4).setUsage(THREE.DynamicDrawUsage));
  glyphGeo.setAttribute('aCol', new THREE.InstancedBufferAttribute(gCol, 4).setUsage(THREE.DynamicDrawUsage));
  glyphGeo.setAttribute('aIdx', new THREE.InstancedBufferAttribute(gIdx, 1).setUsage(THREE.DynamicDrawUsage));
}
const atlas = buildAtlas();
const glyphMat = new THREE.ShaderMaterial({
  uniforms: { uRes: { value: new THREE.Vector2(1, 1) }, uAtlas: { value: atlas } },
  vertexShader: /* glsl */`
    attribute vec4 aRect; attribute vec4 aCol; attribute float aIdx;
    uniform vec2 uRes;
    varying vec2 vUv; varying vec4 vCol;
    void main(){
      vec2 px = aRect.xy + position.xy * aRect.zw;
      float col = mod(aIdx, ${COLS}.0), row = floor(aIdx / ${COLS}.0);
      vec2 t = vec2(col * ${PITCH_X}.0 + ${PAD}.0 + (position.x + 0.5) * ${CW}.0,
                    row * ${PITCH_Y}.0 + ${PAD}.0 + (position.y + 0.5) * ${CH}.0);
      vUv = vec2(t.x / ${ATLAS_W}.0, 1.0 - t.y / ${ATLAS_H}.0);
      vCol = aCol;
      gl_Position = vec4(px.x / uRes.x * 2.0 - 1.0, 1.0 - px.y / uRes.y * 2.0, 0.0, 1.0);
    }`,
  fragmentShader: /* glsl */`
    precision highp float;
    uniform sampler2D uAtlas;
    varying vec2 vUv; varying vec4 vCol;
    void main(){
      float a = texture2D(uAtlas, vUv).r * vCol.a;
      gl_FragColor = vec4(vCol.rgb * a, a);
    }`,
  transparent: true, depthTest: false, depthWrite: false, side: THREE.DoubleSide,
  blending: THREE.CustomBlending, blendEquation: THREE.AddEquation,
  blendSrc: THREE.OneFactor, blendDst: THREE.OneMinusSrcAlphaFactor,
  blendSrcAlpha: THREE.OneFactor, blendDstAlpha: THREE.OneMinusSrcAlphaFactor,
});
const glyphMesh = new THREE.Mesh(glyphGeo, glyphMat);
glyphMesh.frustumCulled = false; glyphMesh.renderOrder = 1;

/* ---------- thermal field: one full-screen shader ---------- */
const MAXA = 8, MAXM = 5;
const v4s = (n) => Array.from({ length: n }, () => new THREE.Vector4());
const U = {
  uRes: { value: new THREE.Vector2(1, 1) }, uDpr: { value: 1 }, uTime: { value: 0 },
  uAg: { value: v4s(MAXA) }, uAgB: { value: v4s(MAXA) },
  uSec: { value: v4s(4) }, uSecFx: { value: v4s(4) },
  uC: { value: new THREE.Vector2() }, uCoreSz: { value: new THREE.Vector2(140, 100) }, uCoreHot: { value: 0 },
  uHive: { value: new THREE.Vector2(300, 150) },
  uBadge: { value: new THREE.Vector4() },
  uRet: { value: new THREE.Vector4() }, uRetB: { value: new THREE.Vector4() },
  uBand: { value: new THREE.Vector4() }, uBandA: { value: 0 },
  uChan: { value: new THREE.Vector4() }, uChanC: { value: new THREE.Vector3(0.42, 0.32, 0.8) }, uChanA: { value: 0 }, uChanV: { value: 0 },
  uMk: { value: v4s(MAXM) }, uMkC: { value: v4s(MAXM) },
  uLz: { value: v4s(3) }, uLzP: { value: new THREE.Vector4(-1, -1, -1, 0) }, uLzC: { value: new THREE.Vector3(1, 0.1, 0.04) },
  uImp: { value: new THREE.Vector4() }, uImpC: { value: new THREE.Vector3(1, 0.5, 0.1) },
  uThr: { value: new Float32Array(MAXA) }, uCoord: { value: new THREE.Vector4(0, 0.6, 0, 0) }, uCoordC: { value: new THREE.Vector3(0.45, 0.65, 1) }, uRetK: { value: 1 },
};
const fieldMat = new THREE.ShaderMaterial({
  uniforms: U, depthTest: false, depthWrite: false,
  vertexShader: `void main(){ gl_Position = vec4(position.xy, 0.0, 1.0); }`,
  fragmentShader: /* glsl */`
    precision highp float;
    uniform vec2 uRes; uniform float uDpr; uniform float uTime;
    uniform vec4 uAg[${MAXA}]; uniform vec4 uAgB[${MAXA}];
    uniform vec4 uSec[4]; uniform vec4 uSecFx[4];
    uniform vec2 uC; uniform vec2 uCoreSz; uniform float uCoreHot; uniform vec2 uHive;
    uniform vec4 uBadge;
    uniform vec4 uRet; uniform vec4 uRetB;
    uniform vec4 uBand; uniform float uBandA;
    uniform vec4 uChan; uniform vec3 uChanC; uniform float uChanA; uniform float uChanV;
    uniform vec4 uMk[${MAXM}]; uniform vec4 uMkC[${MAXM}];
    uniform vec4 uLz[3]; uniform vec4 uLzP; uniform vec3 uLzC;
    uniform vec4 uImp; uniform vec3 uImpC;
    uniform float uThr[${MAXA}]; uniform vec4 uCoord; uniform vec3 uCoordC; uniform float uRetK;

    float hash(vec2 p){ p = fract(p * vec2(123.34, 456.21)); p += dot(p, p + 45.32); return fract(p.x * p.y); }
    float noise(vec2 p){ vec2 i = floor(p), f = fract(p); f = f * f * (3.0 - 2.0 * f);
      return mix(mix(hash(i), hash(i + vec2(1.0, 0.0)), f.x), mix(hash(i + vec2(0.0, 1.0)), hash(i + vec2(1.0, 1.0)), f.x), f.y); }
    float fbm(vec2 p){ return noise(p) * 0.6 + noise(p * 2.1 + 7.3) * 0.3 + noise(p * 4.3 + 1.7) * 0.1; }
    vec3 heat(float h){
      h = clamp(h, 0.0, 1.0);
      vec3 c = mix(vec3(0.0, 0.0, 0.035), vec3(0.06, 0.05, 0.42), smoothstep(0.0, 0.22, h));
      c = mix(c, vec3(0.66, 0.04, 0.60), smoothstep(0.22, 0.45, h));
      c = mix(c, vec3(1.0, 0.38, 0.05), smoothstep(0.45, 0.66, h));
      c = mix(c, vec3(1.0, 0.88, 0.18), smoothstep(0.66, 0.84, h));
      c = mix(c, vec3(1.0, 1.0, 0.95), smoothstep(0.84, 1.0, h));
      return c;
    }
    vec4 hexC(vec2 p){
      const vec2 s = vec2(1.0, 1.7320508);
      vec4 hC = floor(vec4(p, p - vec2(0.5, 1.0)) / s.xyxy) + 0.5;
      vec4 h = vec4(p - hC.xy * s, p - (hC.zw + 0.5) * s);
      return dot(h.xy, h.xy) < dot(h.zw, h.zw) ? vec4(h.xy, hC.xy * s) : vec4(h.zw, (hC.zw + 0.5) * s);
    }
    float sdSeg(vec2 p, vec2 a, vec2 b){ vec2 pa = p - a, ba = b - a; float k = clamp(dot(pa, ba) / dot(ba, ba), 0.0, 1.0); return length(pa - ba * k); }
    float sdTri(vec2 p, float r){ // equilateral; y runs down on screen, so it points down
      const float k = 1.7320508; p.x = abs(p.x) - r; p.y = p.y + r / k;
      if (p.x + k * p.y > 0.0) p = vec2(p.x - k * p.y, -k * p.x - p.y) / 2.0;
      p.x -= clamp(p.x, -2.0 * r, 0.0); return -length(p) * sign(p.y); }
    float sdHex(vec2 p, float r){ const vec3 k = vec3(-0.866025404, 0.5, 0.577350269); p = abs(p);
      p -= 2.0 * min(dot(k.xy, p), 0.0) * k.xy; p -= vec2(clamp(p.x, -k.z * r, k.z * r), r); return length(p) * sign(p.y); }

    void main(){
      vec2 p = vec2(gl_FragCoord.x, uRes.y * uDpr - gl_FragCoord.y) / uDpr;
      float t = uTime;

      float cs = 7.0;
      vec4 hx = hexC(p / cs);
      vec2 c = hx.zw * cs;
      vec2 hl = abs(hx.xy);
      float hedge = max(dot(hl, vec2(0.5, 0.8660254)), hl.x);
      float cell = smoothstep(0.5, 0.39, hedge);
      float ch = hash(hx.zw);

      vec2 q = c - uC;
      float ax = abs(q.x) * uRes.y, ay = abs(q.y) * uRes.x;
      int sid = ay > ax ? (q.y < 0.0 ? 0 : 2) : (q.x > 0.0 ? 1 : 3);
      bool vert = (sid == 0 || sid == 2);
      float r = vert ? abs(q.y) / (uRes.y * 0.5) : abs(q.x) / (uRes.x * 0.5);
      float perp = vert ? q.x : q.y;
      float span = vert ? uRes.x : uRes.y;
      vec4 w = uSec[sid]; vec4 fx = uSecFx[sid];
      float fs = float(sid);

      float n = fbm(c * 0.0045 + vec2(t * 0.02, -t * 0.013) + fs * 17.0);
      float iso = abs(fract(n * 9.0 + t * 0.03) - 0.5);
      float isoL = smoothstep(0.07, 0.0, iso);
      float hAlive = 0.13 + 0.15 * n + isoL * 0.14 + 0.03 * sin(t * 0.6 + fs);
      float hDorm = 0.015 + step(0.962, ch) * 0.10;
      float hHunt = 0.08 + 0.07 * n;
      float hWound = 0.07 + 0.10 * n;
      float h = w.x * hAlive + w.y * hWound + w.z * hHunt + w.w * hDorm;
      vec3 extra = vec3(0.0);

      float cv = fract((r + abs(perp) / span * 0.55) * 8.0 - fx.z);
      float chev = smoothstep(0.16, 0.0, abs(cv - 0.5)) * smoothstep(0.10, 0.24, r);
      extra += w.z * vec3(1.0, 0.60, 0.07) * chev * (0.30 + 0.70 * step(0.30, ch)) * 0.85;

      float zig = (abs(fract(r * 6.0) - 0.5) - 0.25) * span * 0.20 + (noise(vec2(r * 23.0, 3.0 + fs)) - 0.5) * span * 0.08;
      float cd = abs(perp - zig);
      float rr0 = smoothstep(0.10, 0.22, r) * smoothstep(1.02, 0.86, r);
      float flick = 0.62 + 0.38 * step(0.5, fract(t * 3.1 + ch * 0.25));
      extra += w.y * vec3(1.0, 0.05, 0.03) * (exp(-cd * cd / 420.0) * 1.25 * flick * rr0 + step(0.88, fract(ch + floor(t * 4.0) * 0.37)) * 0.45 + 0.20 + 0.16 * n);
      extra += w.y * vec3(1.0, 0.72, 0.50) * exp(-cd * cd / 16.0) * rr0;
      extra += fx.y * vec3(1.0, 0.04, 0.02) * (0.25 + 0.45 * step(0.5, fract(t * 6.0)));

      float wave = exp(-(r - fx.x) * (r - fx.x) / 0.014);
      h += wave * 0.95 + fx.w * 0.46 * (1.0 - r * 0.7);

      // spawns: the running agents, small heat bodies in the hive ring
      vec3 red = vec3(0.0);
      for (int i = 0; i < ${MAXA}; i++) {
        vec4 a = uAg[i]; vec4 b = uAgB[i];
        if (a.w < 0.5) continue;
        vec2 d = c - a.xy; float len = length(d);
        if (len > a.w * 4.2) continue;
        float ang = atan(d.y, d.x);
        float wob = 1.0 + b.w * (0.24 * sin(ang * 3.0 + b.y + t * 1.3) + 0.13 * sin(ang * 5.0 - b.y * 2.0 - t * 0.9));
        float rr = len / (a.w * wob);
        h += a.z * (exp(-rr * rr * 2.0) * 1.06 + 0.20 * exp(-rr * 1.0));
        float rays = pow(abs(sin(ang * 4.0 + b.y + t * 2.2)), 7.0);
        red += b.x * vec3(1.0, 0.06, 0.03) * (exp(-rr * rr * 1.1) * 1.3 + rays * exp(-rr * 0.9) * 1.1) * (0.70 + 0.30 * sin(t * 17.0 + b.y));
      }

      h = floor(h * 15.0 + ch * 0.75) / 15.0;
      vec3 col = (heat(h) * (0.80 + 0.20 * ch) + extra + red) * cell;

      // ---- crisp layer, per pixel ----
      vec2 q2 = p - uC;
      float seam = abs(abs(q2.x) * uRes.y - abs(q2.y) * uRes.x) / length(uRes);
      col *= smoothstep(13.0, 23.0, seam);
      col += vec3(0.30, 0.14, 0.55) * smoothstep(1.1, 0.0, abs(seam - 23.0)) * 0.55;

      // the hive: a faint dotted ellipse where the spawns live
      vec2 qh = q2 / uHive; float fh = length(qh);
      float dh = (fh - 1.0) * fh / max(length(q2 / (uHive * uHive)), 1e-4);
      float hdots = step(0.62, fract(atan(q2.y * uHive.x, q2.x * uHive.y) * 30.0 / 3.14159 - t * 0.02));
      col += vec3(0.36, 0.22, 0.70) * smoothstep(1.2, 0.2, abs(dh)) * hdots * 0.75;
      col *= mix(1.0, 0.72, smoothstep(26.0, 0.0, abs(dh)));

      // core: the focus plate
      float dd = abs(q2.x) / uCoreSz.x + abs(q2.y) / uCoreSz.y;
      float dpx = (dd - 1.0) / length(1.0 / uCoreSz);
      vec3 coreC = mix(vec3(0.30, 0.55, 1.0), vec3(1.0, 0.36, 0.05), uCoreHot);
      float pulse = 1.0 + uCoreHot * 0.35 * sin(t * 2.4);
      col *= smoothstep(0.0, 9.0, dpx);
      col += coreC * smoothstep(1.4, 0.0, abs(dpx)) * pulse;
      float dash = step(0.55, fract(atan(q2.y, q2.x) * 9.0 / 3.14159 + t * 0.04));
      col += coreC * smoothstep(1.0, 0.0, abs(dpx - 8.0)) * dash * 0.7;
      float scanY = fract(t * 0.09) * 2.6 - 1.3;
      col += coreC * 0.07 * exp(-pow(q2.y / uCoreSz.y - scanY, 2.0) * 90.0) * step(dd, 0.97);
      col += coreC * 0.035 * step(dd, 0.97) * (0.5 + 0.5 * sin(dd * 40.0 - t * 1.2)) * smoothstep(0.5, 0.97, dd);

      // the coordinator's body: a soft nucleus, two bright pupils in the plate tips, a counter-rotating segmented ring
      vec3 kc = uCoordC; float ka = uCoord.y;
      vec2 qn = q2 / uCoreSz;
      col += kc * 0.07 * ka * exp(-dot(qn, qn) * 2.2) * step(dd, 0.97);
      for (int s2 = 0; s2 < 2; s2++) {
        vec2 pp = q2 - vec2((s2 == 0 ? -1.0 : 1.0) * (uCoreSz.x - 30.0), 0.0);
        float pd = abs(pp.x) / 9.0 + abs(pp.y) / 5.0;
        col += kc * (smoothstep(1.0, 0.6, pd) * 1.1 + exp(-dot(pp, pp) / 90.0) * 0.6) * (0.45 + 0.75 * ka);
      }
      vec2 rr2 = uCoreSz + vec2(15.0, 11.0);
      vec2 qe = q2 / rr2; float fe = length(qe);
      float de = (fe - 1.0) * fe / max(length(q2 / (rr2 * rr2)), 1e-4);
      float ae = atan(q2.y * rr2.x, q2.x * rr2.y);
      float segE = step(0.38, fract(ae * 6.0 / 6.2831853 + uCoord.x));
      col += kc * smoothstep(1.3, 0.2, abs(de)) * segE * (0.25 + 0.75 * ka);

      // threads: the coordinator to every spawn it leads
      for (int i = 0; i < ${MAXA}; i++) {
        vec4 ag = uAg[i]; if (ag.w < 0.5) continue;
        vec2 ab = ag.xy - uC; float L2 = dot(ab, ab);
        float k = clamp(dot(q2, ab) / L2, 0.0, 1.0);
        float d = length(q2 - ab * k);
        if (d > 5.0) continue;
        float th = abs(uThr[i]), dir = uThr[i] < 0.0 ? -1.0 : 1.0;
        float outside = smoothstep(1.02, 1.3, dd) * smoothstep(1.0, 0.0, (k * sqrt(L2) - (sqrt(L2) - ag.w * 1.2)) / 8.0);
        float dsh = step(0.45, fract(k * sqrt(L2) / 9.0 - t * (0.3 + 3.0 * th) * dir));
        col = mix(col, col * 0.45, smoothstep(3.5, 1.0, d) * outside * 0.6);
        col += mix(kc, vec3(1.0, 0.8, 0.45), th) * smoothstep(1.5, 0.3, d) * outside * (0.32 + 1.1 * th) * (0.5 + 0.5 * dsh) + vec3(1.0, 0.85, 0.6) * th * exp(-d * d / 6.0) * outside * 0.6;
      }

      // badge on the top vertex: how many marks wait
      float bs = sdHex((p - uBadge.xy).yx, uBadge.z);
      vec3 bc = mix(vec3(0.30, 0.55, 1.0), vec3(1.0, 0.45, 0.08), uBadge.w);
      col *= smoothstep(-1.0, 1.0, bs);
      col += bc * smoothstep(1.3, 0.2, abs(bs)) * (0.85 + 0.35 * uBadge.w * sin(t * 2.4));
      col += bc * 0.10 * step(bs, 0.0) * uBadge.w;

      // ticker band at the top edge
      float bd = max(abs(p.x - uBand.x) - uBand.z, abs(p.y - uBand.y) - uBand.w);
      col *= mix(1.0, 0.06 + 0.94 * smoothstep(0.0, 40.0, bd), uBandA);

      // marks: inverted triangles pinned on what needs the human
      for (int i = 0; i < ${MAXM}; i++) {
        vec4 m = uMk[i]; if (m.w < 0.01) continue;
        vec2 d = p - m.xy; float dl = dot(d, d);
        if (dl > 9000.0) continue;
        vec3 mc = uMkC[i].rgb;
        float sd = sdTri(d, m.z);
        float ph = fract(t * 0.42 + float(i) * 0.31);
        col *= mix(1.0, 0.12, smoothstep(1.0, -2.0, sd) * m.w);
        col += mc * smoothstep(1.6, 0.2, abs(sd)) * m.w * 1.35;
        col += mc * smoothstep(1.2, 0.2, abs(sdTri(d, m.z + 4.0 + ph * 12.0))) * m.w * 0.55 * (1.0 - ph);
        col += mc * exp(-dl / (m.z * m.z * 2.4)) * 0.22 * m.w;
        col += vec3(1.0, 0.92, 0.75) * uMkC[i].w * exp(-dl / (m.z * m.z * 1.2)) * 2.2;
      }

      // command channel: a hatch in the bottom edge (a handle at rest, a panel when open)
      vec2 kq = abs(p - uChan.xy);
      float kd = max(max(kq.x - uChan.z, kq.y - uChan.w), (kq.x + kq.y - (uChan.z + uChan.w - 18.0)) * 0.7071);
      col *= 0.35 + 0.65 * smoothstep(0.0, 22.0, kd);
      col *= mix(0.09, 1.0, smoothstep(-1.0, 1.0, kd));
      vec2 kp = p - uChan.xy;
      col += uChanC * smoothstep(1.3, 0.25, abs(kd)) * (0.40 + 0.60 * uChanA);
      col += uChanC * smoothstep(1.1, 0.2, abs(kd + 5.0)) * step(0.55, fract((kp.x + abs(kp.y)) / 11.0 - t * (0.2 + 1.4 * uChanA))) * (0.12 + 0.38 * uChanA);
      col += vec3(1.0, 0.14, 0.04) * uChanV * smoothstep(-16.0, -1.0, kd) * step(kd, 0.0) * (0.16 + 0.10 * sin(t * 11.0));

      // flag ring: a spawn that waits for the human
      for (int i = 0; i < ${MAXA}; i++) {
        float f = uAgB[i].z;
        if (f < 0.01 || uAg[i].w < 0.5) continue;
        vec2 d = p - uAg[i].xy; float len = length(d);
        float rad = uAg[i].w * 1.45 + (1.0 - f) * 30.0 + 2.0 * sin(t * 3.0);
        float seg = step(0.30, fract(atan(d.y, d.x) * 3.0 / 3.14159 - t * 0.15));
        col += vec3(1.0, 0.66, 0.10) * f * smoothstep(1.6, 0.3, abs(len - rad)) * seg * (0.7 + 0.3 * sin(t * 3.0));
      }

      // lasers
      for (int i = 0; i < 3; i++) {
        float pr = uLzP[i];
        if (pr <= 0.0 || pr >= 2.0) continue;
        vec2 a = uLz[i].xy, b = uLz[i].zw;
        float hd = min(pr, 1.0), fade = 1.0 - clamp(pr - 1.0, 0.0, 1.0);
        vec2 hp = mix(a, b, hd);
        vec2 tp = mix(a, b, clamp(pr - 0.55, 0.0, 1.0));
        float d = sdSeg(p, tp, hp);
        col += uLzC * exp(-d * d / 14.0) * 1.5 * fade + vec3(1.0, 0.95, 0.85) * smoothstep(1.0, 0.15, d) * fade;
        vec2 hq = p - hp;
        col += vec3(1.0, 0.85, 0.6) * exp(-dot(hq, hq) / 30.0) * 2.0 * step(pr, 1.0);
      }
      // impact
      if (uImp.w > 0.0) {
        vec2 d = p - uImp.xy; float l = length(d), age = uImp.z;
        col += uImpC * smoothstep(3.0, 0.0, abs(l - (8.0 + age * 140.0))) * max(0.0, 1.0 - age / 0.55) * 1.6;
        col += vec3(1.0, 0.95, 0.85) * exp(-l * l / (80.0 + age * 900.0)) * max(0.0, 1.0 - age / 0.32) * 2.4;
      }

      // three-dot reticle
      vec2 rp = p - uRet.xy;
      vec3 rc = mix(vec3(1.0, 0.07, 0.03), vec3(1.0, 0.68, 0.10), uRetB.z);
      float rl = length(rp);
      if (rl < uRet.z * 2.2 + 60.0) {
        float dots = 0.0, tri = 0.0;
        vec2 prev = vec2(cos(uRet.w - 2.0943951), sin(uRet.w - 2.0943951)) * uRet.z;
        for (int i = 0; i < 3; i++) {
          float an = uRet.w + float(i) * 2.0943951;
          vec2 dp = vec2(cos(an), sin(an)) * uRet.z;
          float dl = length(rp - dp);
          dots += smoothstep(3.6, 2.2, dl) + 0.55 * exp(-dl * dl / 46.0);
          tri += smoothstep(0.9, 0.1, sdSeg(rp, prev, dp));
          prev = dp;
        }
        float aa = atan(rp.y, rp.x) - uRet.w;
        float m = abs(fract(aa / 2.0943951) - 0.5);
        float arcR = uRet.z * 1.9 + 8.0;
        float arcs = smoothstep(1.5, 0.4, abs(rl - arcR)) * step(m, 0.30);
        float m0 = abs(fract(aa / 2.0943951 + 0.5) - 0.5) * 2.0943951 * rl;
        float ticks = smoothstep(1.3, 0.3, m0) * step(arcR + 5.0, rl) * step(rl, arcR + 26.0);
        col += rc * uRetB.y * (dots + (tri * 0.22 + (arcs + ticks) * uRetB.x) * uRetK);
        col += rc * uRetB.w * exp(-rl * rl / 2600.0) * 1.4;
      }

      gl_FragColor = vec4(col, 1.0);
    }`,
});
const fieldMesh = new THREE.Mesh(new THREE.BufferGeometry().setAttribute('position',
  new THREE.BufferAttribute(new Float32Array([-1, -1, 0, 3, -1, 0, -1, 3, 0]), 3)), fieldMat);
fieldMesh.frustumCulled = false; fieldMesh.renderOrder = 0;
scene.add(fieldMesh, glyphMesh);


/* ---------- model ---------- */
// sector weights: [alive, wounded, on the hunt, dormant]
const ALIVE = [1, 0, 0, 0], WOUNDED = [0, 1, 0, 0], HUNT = [0, 0, 1, 0], DORMANT = [0, 0, 0, 1];
const LOOK = { alive: ALIVE, wounded: WOUNDED, hunt: HUNT, dormant: DORMANT };
const sectors = [0, 1, 2, 3].map((i) => ({
  letter: '', w: DORMANT.slice(), target: DORMANT.slice(), wave: -1, after: 0, alarm: 0, phase: 0, speed: 0.35, speedT: 0.35,
  flare: 0, tally: 0, stampT: -9, sigil: [5, 17, 29, 41][i], paused: true, p: null, look: null, relOpen: false,
}));
// the sector's name on the field: the ASCII part of the project's tag («🌐 DM» → «CH»)
const SL = (s) => (s == null || s < 0 ? '' : sectors[s]?.letter || '·');
const shortTag = (tag) => (String(tag || '').match(/[A-Za-z0-9]+/g) || ['?']).join(' ').slice(0, 5);
// only what the atlas can draw: emoji and other marks are dropped, the Ukrainian apostrophe becomes '
const clean = (s) => Array.from(String(s ?? '').replace(/[ʼ’]/g, "'").replace(/\s+/g, ' '))
  .filter((c) => c === ' ' || CHAR_IDX.has(c)).join('').replace(/\s+/g, ' ').trim();
const cut = (s, n) => (s.length > n ? s.slice(0, n - 1) + '…' : s);

// Spawns ARE the agents: they live in one ring (the hive) around the focus plate,
// each on its project's side, so «how many run and for whom» is one look.
const agents = [];
let agentNo = 0, now = 0;
function spawn(s, opts = {}) {
  if (agents.filter((a) => a.dieT < 0).length >= MAXA) return null;
  const n = ++agentNo, a = {
    id: n, name: `AGENT-${n}`, s, seed: n * 2.39 + 0.7, ph: n * 1.7, x: L.cx, y: L.cy, init: false,
    heat: 0, heatT: 1, heatK: 3, move: 1, moveT: 1, red: 0, redT: 0, flag: 0, flagT: 0,
    born: now, dieT: -1, life: 0, reqT: -9, state: 'work', tagT0: now + 0.25,
  };
  Object.assign(a, opts);
  agents.push(a); boost(1.6);
  return a;
}
function endAgent(a) { if (a && a.dieT < 0) { a.dieT = now; boost(1.6); } }
const liveAgents = () => agents.filter((a) => a.dieT < 0);
function setAgentState(a, s) {
  a.state = s;
  const P = { work: [1, 3, 1, 0, 0], wait: [0.75, 3, 0.5, 0, 1], hung: [0.28, 0.7, 0, 0, 0], fail: [0.45, 4, 0.6, 1, 0], done: [0.5, 2, 0.15, 0, 0] }[s];
  a.heatT = P[0]; a.heatK = P[1]; a.moveT = P[2]; a.redT = P[3]; a.flagT = P[4];
}

const ret = { x: 0, y: 0, spread: 22, spreadT: 22, rot: 0, rotV: 0.4, lock: 0, lockT: 0, alpha: 0.75, amber: 0, amberT: 0, fire: 0 };
const ROT0 = Math.PI * 7 / 6; // dot 1 upper left, dot 2 upper right, dot 3 below
const perim = { r: 1, g: 0.1, b: 0.05, amt: 0, t0: 0 };
let coreHot = 0, bandA = 0;
let hotUntil = 0;
const boost = (sec) => { hotUntil = Math.max(hotUntil, now + sec); };

/* ---------- live data (set by attach) ---------- */
let store = { s: {} }, api = null, shown = false;
const D = () => store.s || {};
const W2 = { uk: { reply: 'ВІДПОВІДЬ', full: 'ПОВНА ВІДПОВІДЬ — У ТЕРМІНАЛІ' }, en: { reply: 'REPLY', full: 'THE FULL REPLY IS IN THE TERMINAL' } };

/* ---------- DOM ---------- */
const $ = (id) => document.getElementById(id);
const elSys = $('sys'), elPerf = $('perf'), elKeys = $('keys'), elDry = $('dry');
let lastSlow = -9;
const hm = (d) => `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
function slowDom() {
  const s = D(), q = s.heavy || {};
  const heavy = (q.ml || []).length ? `HEAVY · LOCAL MODEL ${q.ml.length}` : (q.holders || (q.holder ? [q.holder] : [])).length
    ? `HEAVY · ${(q.holders || [q.holder]).length} RUNS · ${(q.waiting || []).length} WAIT` : 'HEAVY · FREE';
  const models = (s.models?.providers || []).map((p) => p.name === 'Ollama' ? `OLLAMA ${p.up ? 'UP' : 'DOWN'}`
    : `${p.name.toUpperCase()} ${Object.entries(p.limits || {}).map(([k, l]) => `${k} ${l.used}%`).join(' ') || '—'}`);
  elSys.textContent = [hm(new Date()), s.status_at ? `DATA ${hm(new Date(s.status_at * 1000))}` : 'DATA …', heavy, ...models].join(' · ');
  elPerf.textContent = `${perf.fps.toFixed(0)} fps · ${perf.fullFps.toFixed(0)} full · ${perf.ms.toFixed(1)} ms · ${perf.calls} calls`;
  elDry.hidden = !s.dry_run;
}
const perf = { fps: 0, fullFps: 0, ms: 0, calls: 0 };

/* ---------- layout ---------- */
const cmdEl = document.getElementById('cmd');
function resize() {
  W = window.innerWidth; H = window.innerHeight;
  DPR = Math.min(window.devicePixelRatio || 1, 2);
  renderer.setPixelRatio(DPR);
  renderer.setSize(W, H, false);
  const m = Math.min(W, H);
  L.cx = W / 2; L.cy = H / 2 - Math.min(24, H * 0.03); L.m = m;
  L.coreX = clamp(W * 0.165, 165, 250); L.coreY = clamp(L.coreX * 0.4, 68, 96);
  L.hiveX = L.coreX + 70; L.hiveY = L.coreY + 42;
  L.agentR = clamp(m * 0.029, 18, 26);
  U.uRes.value.set(W, H); U.uDpr.value = DPR;
  U.uC.value.set(L.cx, L.cy); U.uCoreSz.value.set(L.coreX, L.coreY); U.uHive.value.set(L.hiveX, L.hiveY);
  U.uBadge.value.set(L.cx, L.cy - L.coreY - 2, 15, 0);
  glyphMat.uniforms.uRes.value.set(W, H);
  // channel: a hatch in the bottom edge
  L.chHW = clamp(W * 0.27, 250, 440); L.handleHW = 46; L.handleH = 14;
  L.chL = L.cx - L.chHW; L.chR = L.cx + L.chHW;
  // the open panel stops above the hive, so a spawn that hatches stays visible
  L.fFont = 14; L.fRow = L.fFont * 1.2 + 6.5;
  const minTop = L.cy + L.hiveY + L.agentR + 36;
  L.maxRows = clamp(Math.floor((H - 18 - minTop - 20 - 50) / L.fRow), 3, 6);
  L.panelH = L.maxRows * L.fRow + 20 + 50;
  // the conversation: a readable block, its top below the coordinator's plate
  L.convH = clamp(Math.round(Math.min(H * 0.46, H - 18 - (L.cy + L.coreY + 34))), 220, 560);
  L.inY = H - 18 - 25; L.fTop = H - 18 - L.panelH + 20;
  L.fLeft = L.chL + 34; L.fMaxChars = Math.floor((L.chR - 30 - L.fLeft) / (L.fFont * 0.7));
  L.scis = { x: L.chL - 26, y: 0, w: L.chHW * 2 + 52, h: 18 + 28 + 30 };
  Object.assign(cmdEl.style, { left: L.chL + 'px', top: (L.inY - 20) + 'px', width: L.chHW * 2 + 'px', height: '40px' });
  L.sig = [[L.cx - clamp(W * 0.17, 200, 280), 108], [W - 84, L.cy], [W - 84, H - 92], [84, L.cy]];
  L.sig[2] = [L.chR + 92, H - 84];
  feed.forEach((l) => { l.y = null; });
  boost(0.3);
}

/* ---------- hive positions ---------- */
const SEC_ANG = [-Math.PI / 2, 0, Math.PI / 2, Math.PI];
// Each spawn roams its own zone: its project's arc of the hive ring, a little in and out.
const zoneOf = (s) => {
  const tSeam = Math.atan((H / W) * (L.hiveX / L.hiveY)), m = 0.22;
  return { c: SEC_ANG[s], span: s === 0 || s === 2 ? Math.PI / 2 - tSeam - m : tSeam - m, r0: 0.97, r1: 1.2 };
};
const angD = (a, b) => { let d = a - b; while (d > Math.PI) d -= TAU; while (d < -Math.PI) d += TAU; return d; };
const toZone = (z, th, rho) => [L.cx + Math.cos(z.c + th) * L.hiveX * rho, L.cy + Math.sin(z.c + th) * L.hiveY * rho];
const fromZone = (z, x, y) => { const qx = (x - L.cx) / L.hiveX, qy = (y - L.cy) / L.hiveY; return [angD(Math.atan2(qy, qx), z.c), Math.hypot(qx, qy)]; };
function newGoal(a, z) { a.gTh = (Math.random() * 2 - 1) * z.span * 0.9; a.gRho = lerp(z.r0, z.r1, Math.random()); a.gT = now + 3 + Math.random() * 4; }
function placeAgents(dt) {
  const live = agents.filter((a) => a.dieT < 0 || now - a.dieT < 0.9);
  for (const a of live) {
    const z = zoneOf(a.s);
    if (!a.init) { // first place: spread over the zone, away from the others
      a.init = true; a.vx = 0; a.vy = 0;
      const k = agents.filter((o) => o.s === a.s && o !== a && o.init).length;
      const th = ((k % 3) - 1) * z.span * 0.6 + (Math.random() - 0.5) * 0.1;
      [a.hx, a.hy] = toZone(z, th, 1.05); newGoal(a, z); a.dartT = now + 4 + Math.random() * 8;
    }
  }
  for (const a of live) {
    const z = zoneOf(a.s);
    const [th, rho] = fromZone(z, a.hx, a.hy);
    let speed = 0, ax = 0, ay = 0, damp = 2.2;
    if (a.hold || a.dieT >= 0) { a.vx = 0; a.vy = 0; continue; }
    if (a.state === 'work' || a.state === 'done') {
      if (now > a.gT || Math.hypot(...[0, 1].map((i) => toZone(z, a.gTh, a.gRho)[i] - [a.hx, a.hy][i])) < 12) newGoal(a, z);
      speed = a.state === 'work' ? 20 : 7;
      if (a.state === 'work' && now > a.dartT) { newGoal(a, z); a.dartEnd = now + 0.55; a.dartT = now + 6 + Math.random() * 8; }
      if (a.dartEnd && now < a.dartEnd) { speed = 95; boost(0.1); }
    } else if (a.state === 'wait') { // slows and circles near the edge closest to the centre
      if (a.anchTh == null) { a.anchTh = clamp(th, -z.span * 0.7, z.span * 0.7); }
      a.gTh = a.anchTh + Math.cos(now * 0.9 + a.seed) * 18 / L.hiveX; a.gRho = z.r0 + 0.02 + Math.sin(now * 0.9 + a.seed) * 14 / L.hiveY;
      speed = 16;
    } else if (a.state === 'hung') { damp = 0.7; }  // drifts to a stop
    else if (a.state === 'fail') { damp = 7; }      // stops at once
    if (a.state !== 'wait') a.anchTh = null;
    if (speed > 0) {
      const g = toZone(z, a.gTh, a.gRho), dx = g[0] - a.hx, dy = g[1] - a.hy, d = Math.hypot(dx, dy) || 1;
      const sp = speed * clamp(d / 40, 0.25, 1);
      ax += (dx / d * sp - a.vx) * 1.6; ay += (dy / d * sp - a.vy) * 1.6;
    } else { ax -= a.vx * damp; ay -= a.vy * damp; }
    for (const o of live) {
      if (o === a || o.hold) continue;
      const dx = a.hx - o.hx, dy = a.hy - o.hy, d = Math.hypot(dx, dy) || 0.01;
      const still = a.state === 'hung' || a.state === 'fail', R0 = still ? 2 * L.agentR + 12 : 2 * L.agentR + 72;
      if (d < R0) { const f = (R0 - d) / R0; ax += dx / d * f * 160; ay += dy / d * f * 160; }
    }
    const cth = clamp(th, -z.span, z.span), crho = clamp(rho, z.r0, z.r1);
    if (cth !== th || crho !== rho) { const c = toZone(z, cth, crho); ax += (c[0] - a.hx) * 6; ay += (c[1] - a.hy) * 6; }
    a.vx += ax * dt; a.vy += ay * dt;
    const v = Math.hypot(a.vx, a.vy), vmax = a.dartEnd && now < a.dartEnd ? 110 : 40;
    if (v > vmax) { a.vx *= vmax / v; a.vy *= vmax / v; }
    a.hx += a.vx * dt; a.hy += a.vy * dt;
    const [th2, rho2] = fromZone(z, a.hx, a.hy);
    if (Math.abs(th2) > z.span + 0.08 || rho2 < z.r0 - 0.04 || rho2 > z.r1 + 0.06) {
      [a.hx, a.hy] = toZone(z, clamp(th2, -z.span - 0.08, z.span + 0.08), clamp(rho2, z.r0 - 0.04, z.r1 + 0.06));
    }
  }
}
const agentR = (a) => L.agentR * (0.8 + 0.2 * a.heat) * a.life;

/* ---------- marks: the notifications live on the field ---------- */
// ci = a problem (failed CI, URL down, blocked ticket, PR conflict); ask = an operation that waits for
// the user; hung = an operation of an agent a restart interrupted.
const KIND = {
  ci:   { pri: 5, col: [1, 0.12, 0.06], glyph: 13, label: 'PROBLEM' },
  hung: { pri: 4, col: [0.45, 0.62, 1], glyph: 33, label: 'INTERRUPTED' },
  ask:  { pri: 3, col: [1, 0.64, 0.10], glyph: 7,  label: 'DECISION' },
};
const marks = [];
let markNo = 0;
const openMarks = () => marks.filter((m) => m.burnT < 0 && (!m.agent || m.agent.dieT < 0))
  .sort((a, b) => KIND[b.kind].pri - KIND[a.kind].pri || a.order - b.order);
const KOFF = { ci: 0, hung: 1, ask: 2 };
function markPos(m) {
  const a = m.agent;
  if (a && a.dieT < 0) {
    const R = L.agentR + 10;
    if (a.s === 0) return [a.x, a.y - R - 44];
    if (a.s === 2) return [a.x, a.y + R + 44];
    if (a.s === 1) return [a.x + R + 30, a.y - 44];
    return [a.x - R - 30, a.y - 44];
  }
  // marks of one sector stand in a short row by kind: problems, interrupted, decisions
  const [sx, sy] = L.sig[m.s], k = KOFF[m.kind] * 50;
  return [[sx + 80 + k, sy], [sx - 84, sy - 60 + k], [sx - 84 - k, sy], [sx + 84, sy - 60 + k]][m.s];
}
// a mark on the field stands for its group (same place): the top item and a count
const groupKey = (m) => (m.agent && m.agent.dieT < 0 ? `a${m.agent.id}` : `s${m.s}${m.kind}`);
function groups() {
  const g = new Map();
  for (const m of openMarks()) { const k = groupKey(m); if (!g.has(k)) g.set(k, { top: m, n: 0 }); g.get(k).n++; }
  return [...g.values()];
}

/* ---------- lock: what the reticle holds ---------- */
let lk = null, armed = null;
function lockPos(l) {
  if (!l) return null;
  if (l.kind === 'mark') return markPos(l.m);
  if (l.kind === 'agent') return [l.a.x, l.a.y];
  if (l.kind === 'core') return [L.cx, L.cy];
  return L.sig[l.s];
}
const lockSec = (l) => !l || l.kind === 'core' ? null : l.kind === 'mark' ? l.m.s : l.kind === 'agent' ? l.a.s : l.s;
function setLock(l, by = 'user') {
  if (l && l.kind === 'agent') { const m = openMarks().find((x) => x.agent === l.a); if (m) l = { kind: 'mark', m }; }
  if (l) { l.by = by; l.t0 = now; }
  lk = l; armed = null;
  inp.target = l ? lockSec(l) : null; inp.tgtT = now;
  boost(1.2); hints(); wake();
}

/* ---------- ticker: one decoded line at the top edge ---------- */
let tick = null;
const TK_STEP = 0.018, TK_HOLD = 3.4;
function ticker(text, col) {
  text = cut(clean(text), Math.max(20, Math.floor((W - 120) / 10.5)));
  if (!text) return;
  tick = { text, col: col || [1, 0.8, 0.4], t0: now };
  const end = 0.25 + text.length * TK_STEP + TK_HOLD;
  boost(0.4 + text.length * TK_STEP);
  later(end - 0.05, () => { if (tick && tick.text === text) boost(1.4); });
}
function drawTicker() {
  if (!tick) { return false; }
  const s = tick.text, n = s.length, age = now - tick.t0, f = 15, cw = f * 0.7, ch = f * 1.2;
  const hold = 0.25 + n * TK_STEP + TK_HOLD;
  if (age > hold + 1.3) { tick = null; return false; }
  const x0 = L.cx - (n - 1) * cw / 2, y = 50, t2 = Math.floor(now * 16);
  const fade = clamp(1 - (age - hold - 0.35) / 0.9, 0, 1);
  for (let i = 0; i < n; i++) {
    const c = s[i];
    const latin = age > 0.25 + i * TK_STEP && !(age > hold + (n - 1 - i) * 0.008);
    if (age < i * 0.004) continue;
    if (latin) { if (c !== ' ') { const fl = clamp(1 - (age - 0.25 - i * TK_STEP) / 0.2, 0, 1); glyph(x0 + i * cw, y, cw, ch, latinIdx(c), lerp(i < 2 ? tick.col[0] : 1, 1, fl), lerp(i < 2 ? tick.col[1] : 0.96, 1, fl), lerp(i < 2 ? tick.col[2] : 0.86, 1, fl), fade); } }
    else glyph(x0 + i * cw, y, cw * 0.86, ch * 0.86, alienAt(i + 3, t2), tick.col[0], tick.col[1], tick.col[2], 0.85 * fade);
  }
  U.uBand.value.set(L.cx, y, n * cw / 2 + 14, ch / 2 + 2);
  return age < hold + 0.6;
}

/* ---------- lasers and impact ---------- */
const lz = [null, null, null];
let imp = null;
const LZ_DUR = 0.17, LZ_FADE = 0.32;
function laser(i, from, to, delay = 0) { lz[i] = { from, to, t0: now + delay }; boost(delay + LZ_DUR + LZ_FADE + 0.1); }
function impact(to, col) { imp = { to, t0: now, col }; boost(0.7); }
function updateLasers() {
  for (let i = 0; i < 3; i++) {
    const l = lz[i];
    if (!l) { U.uLzP.value.setComponent(i, -1); continue; }
    const age = now - l.t0;
    if (age < 0) { U.uLzP.value.setComponent(i, -1); continue; }
    const pr = age < LZ_DUR ? age / LZ_DUR : 1 + (age - LZ_DUR) / LZ_FADE;
    if (pr >= 2) { lz[i] = null; U.uLzP.value.setComponent(i, -1); continue; }
    const a = l.from, b = l.to();
    U.uLz.value[i].set(a[0], a[1], b[0], b[1]);
    U.uLzP.value.setComponent(i, Math.max(pr, 0.001));
  }
  if (imp) {
    const age = now - imp.t0, p = imp.to();
    if (age > 0.6) { imp = null; U.uImp.value.w = 0; }
    else { U.uImp.value.set(p[0], p[1], age, 1); U.uImpC.value.set(...imp.col); }
  }
}
const dotPos = (i, rot = ret.rot) => { const an = rot + i * TAU / 3; return [ret.x + Math.cos(an) * ret.spread, ret.y + Math.sin(an) * ret.spread]; };

/* ---------- a new mark arrives as a shot ---------- */
const shotQ = [];
let shot = null;
function notify(m) { shotQ.push(m); wake(); }
function updateShot() {
  if (!shot && shotQ.length && !fx) {
    const m = shotQ.shift();
    if (m.burnT >= 0 || !marks.includes(m)) return;
    shot = { m, t0: now, st: 0, hold: [ret.x, ret.y], to: () => markPos(m) }; boost(2.2); coordThink(1.4);
  }
  if (!shot) return;
  const s = shot, ts = now - s.t0, m = s.m;
  if (s.st === 0 && ts > 0.36) {
    s.st = 1; U.uLzC.value.set(1, 0.1, 0.04);
    for (let i = 0; i < 3; i++) laser(i, dotPos(i), s.to, i * 0.05);
  }
  if (s.st === 1 && ts > 0.36 + 0.1 + LZ_DUR) {
    s.st = 2;
    const K = KIND[m.kind];
    impact(s.to, K.col);
    m.pending = false; m.t0 = now;
    ticker(`${SL(m.s)} · ${m.text}`, K.col);
    if (m.kind === 'ci') { const sc = sectors[m.s]; sc.alarm = 1; Object.assign(perim, { r: 1, g: 0.06, b: 0.03, amt: 1, t0: now }); boost(2.6); }
    ret.fire = 1.2;
  }
  if (s.st === 2 && ts > 0.95) {
    s.st = 3;
    const busy = lk && lk.by === 'user' && now - lk.t0 < 8 && !(lk.kind === 'mark' && lk.m.burnT >= 0);
    if (!busy) setLock({ kind: 'mark', m }, 'auto');
  }
  if (s.st === 3 && ts > 1.45) shot = null;
}

/* ---------- actions: the three dots on a lock (each one an existing command, through the bridge) ---------- */
const dismissed = new Map();               // mark key → until when a fired answer hides it (the data catches up)
const STAGE2 = (label) => ({ label, stage2: true, keep: true, run: () => ticker(`${label} · STAGE 2 · NOT CONNECTED YET`, [0.6, 0.8, 1]) });
async function call(p, okText, col, m) {
  const r = await p;
  if (!r.ok) { ticker(`FAILED · ${r.error || r.msg || 'NO ANSWER FROM THE BRIDGE'}`, [1, 0.3, 0.2]); if (m) { m.burnT = -1; dismissed.delete(m.key); } return r; }
  ticker(r.dry ? `DRY-RUN · LOGGED, NOT RUN · ${okText}` : okText, col || [0.6, 0.8, 1]);
  if (r.dry && m) { m.burnT = -1; dismissed.delete(m.key); }   // nothing changed: the mark stays
  return r;
}
const agentOfMark = (m) => (m.agent && m.agent.dieT < 0 ? m.agent : null);
function openAgentAct(a, label = 'OPEN') {
  return { label, keep: true, run: () => call(api.open(a.addr), `→ ${a.name} TERMINAL`) };
}
function laterAct(m) {
  return { label: 'LATER', run: () => { dismissed.set(m.key, now + 60); call(api.later(m.op), `${SL(m.s)} · PUT ASIDE · ${m.op.id}`, null, m); } };
}
function askAct(m) { return { label: 'ASK', keep: true, run: () => call(api.ask(m.op), `${SL(m.s)} · ASKED THE COORDINATOR`) }; }
function actionsFor(l) {
  if (!l) return null;
  const L2 = SL(lockSec(l));
  if (l.kind === 'mark') {
    const m = l.m, a = agentOfMark(m);
    if (m.op) {
      const acts = (m.op.answers || []).slice(0, 3).map((ans) => ({ label: clean(ans).toUpperCase() || '?', run: () => {
        dismissed.set(m.key, now + 60);
        call(api.answer(m.op, ans), `${L2} · ANSWERED · ${clean(ans).toUpperCase()}`, [1, 0.8, 0.4], m);
        if (a) threadPacket(a, true);
      } }));
      if (acts.length < 3 && a) acts.push(openAgentAct(a));
      if (acts.length < 3) acts.push(laterAct(m));
      if (acts.length < 3) acts.push(askAct(m));
      return acts;
    }
    const p = m.problem;
    return [
      p.url ? { label: 'OPEN LINK', keep: true, run: () => { window.open(p.url, '_blank', 'noopener'); ticker(`${L2} · OPENED IN THE BROWSER`); } } : { label: 'STATUS', keep: true, run: () => showReply(sectorStatus(m.s), { auto: true }) },
      p.data?.kind === 'pr_conflict' ? STAGE2('RESOLVE') : { label: 'STATUS', keep: true, run: () => showReply(sectorStatus(m.s), { auto: true }) },
      { label: 'NEXT', keep: true, run: () => cycle() }];
  }
  if (l.kind === 'core') return [
    { label: 'STATUS', keep: true, run: () => { coordThink(1); showReply({ lines: [0, 1, 2, 3].filter((s) => sectors[s].p).map((s) => sectorLine(s)) }, { auto: true }); } },
    { label: 'FOCUS', keep: true, run: () => showReply(focusReply(), { auto: true }) },
    { label: 'TALK', keep: true, run: () => openChan(true) }];
  if (l.kind === 'agent') return [
    { label: 'STATUS', keep: true, run: () => showReply({ lines: [{ sec: l.a.s, text: agentLine(l.a) }] }, { auto: true }) },
    STAGE2('STOP'),
    openAgentAct(l.a)];
  const sc = sectors[l.s];
  if (!sc.p) return [STAGE2('STATUS'), STAGE2('PAUSE'), STAGE2('MARKS')].map((x) => ({ ...x, label: '—', run: () => {} }));
  return [
    { label: 'STATUS', keep: true, run: () => showReply(sectorStatus(l.s), { auto: true }) },
    { label: sc.p.paused ? 'RESUME' : 'PAUSE', keep: true, run: () => call(api.pause(sc.p), `${SL(l.s)} · ${sc.p.paused ? 'RESUME' : 'PAUSE'} SENT`) },
    { label: 'MARKS', keep: true, run: () => cycle(l.s) }];
}
let fx = null;
function fire(k) {
  const acts = actionsFor(lk);
  if (!acts || shot || fx || ret.lock < 0.6) { flashKeys(); return; }
  const act = acts[k - 1];
  if (!act || act.label === '—') { flashKeys(); return; }
  if (act.stage2) { act.run(); flashKeys(); return; }
  if (act.danger && !(armed && armed.k === k && now - armed.t < 4)) { armed = { k, t: now }; boost(0.6); hints(); return; }
  armed = null;
  const l = lk, to = l.kind === 'core' ? coreEdgeTo(k - 1) : () => lockPos(l) || [ret.x, ret.y];
  U.uLzC.value.set(1, 0.45, 0.08);
  ret.spreadT = 44; ret.spread = 44;
  fx = { t0: now, act, l, to, k };
  laser(0, dotPos(k - 1, ROT0 + TAU / 3 * Math.round((ret.rot - ROT0) / (TAU / 3))), to, 0.03);
  hints();
}
// L: «later» on the locked operation (the panel's ⏸ later)
function fireLater() {
  if (!lk || lk.kind !== 'mark' || !lk.m.op || fx || shot) { flashKeys(); return; }
  const act = laterAct(lk.m), l = lk;
  U.uLzC.value.set(0.55, 0.7, 1);
  fx = { t0: now, act, l, to: () => lockPos(l) || [ret.x, ret.y], k: 0 };
  laser(0, [ret.x, ret.y - ret.spread], fx.to, 0.03);
}
function updateFx() {
  if (!fx) return;
  const age = now - fx.t0;
  if (!fx.hit && age >= 0.03 + LZ_DUR) {
    fx.hit = 1; impact(fx.to, fx.act.danger ? [1, 0.2, 0.1] : [1, 0.7, 0.25]); ret.fire = 1;
    fx.act.run();
    if (!fx.act.keep && fx.l.kind === 'mark') { fx.l.m.burnT = now; boost(1); }
  }
  if (age > 0.75) {
    const l = fx.l; fx = null;
    if (lk === l && l.kind === 'mark' && l.m.burnT >= 0) setLock(null);
  }
}

/* ---------- N: cycle the open marks, most important first (in one sector with MARKS) ---------- */
let cyc = { i: -1, t: -9 };
function cycle(sec = null) {
  const open = openMarks().filter((m) => sec == null || m.s === sec);
  if (!open.length) { ticker(sec == null ? 'NO OPEN MARKS' : `${SL(sec)} · NO OPEN MARKS`, [0.55, 0.75, 1]); return; }
  const ids = `${sec}|` + open.map((m) => m.id).join();
  if (now - cyc.t > 6 || cyc.key !== ids) {
    cyc = { list: open, i: -1, key: ids };
    if (lk && lk.kind === 'mark' && lk.by === 'user') cyc.i = open.indexOf(lk.m);
  }
  cyc.t = now;
  cyc.i = (cyc.i + 1) % cyc.list.length;
  const m = cyc.list[cyc.i];
  setLock({ kind: 'mark', m }, 'user');
  ticker(`${cyc.i + 1} OF ${cyc.list.length} · ${SL(m.s)} · ${m.text}`, KIND[m.kind].col);
}

/* ---------- the coordinator ---------- */
const coord = { think: -9, deleg: -9, rot: 0, spd: 0.12, a: 0.6, col: [0.45, 0.65, 1], state: 'idle', ring: 0 };
const coordThink = (sec) => { coord.think = Math.max(coord.think, now + sec); boost(sec); };
const CORE_SPREAD = () => Math.max(L.coreY + 30, 1.12 / (0.866 / L.coreX + 0.5 / L.coreY));
const coreEdgeTo = (i) => () => { const an = ROT0 + i * TAU / 3, dx = Math.cos(an), dy = Math.sin(an), k = 1 / (Math.abs(dx) / L.coreX + Math.abs(dy) / L.coreY); return [L.cx + dx * k, L.cy + dy * k]; };
function threadPacket(a, out) {
  if (!a) return;
  const n = 7, from = out ? [L.cx, L.cy] : [a.x, a.y], pts = [];
  for (let k = 0; k < n; k++) pts.push({ c: ' ', x: from[0] + (k - (n - 1) / 2) * 6, y: from[1], w: 10, h: 17 });
  a.thr = 1; a.thrDir = out ? 1 : -1; boost(1.2);
  launch(pts, out ? () => [a.x, a.y] : () => [L.cx, L.cy], () => { a.thr = 1; if (out) a.reqT = now; else { corePulse = 0.7; coordThink(0.9); } });
}
const COORD_ST = { // speed of the ring (rad/s), brightness, colour
  idle:  [0.10, 0.55, [0.45, 0.65, 1]],
  think: [0.55, 0.95, [0.78, 0.62, 1]],
  speak: [0.25, 0.85, [0.55, 0.88, 1]],
  deleg: [0.40, 1.00, [1, 0.62, 0.22]],
  wait:  [0.00, 0.55, [1, 0.70, 0.16]],
};
function updateCoord(dt) {
  const speaking = chan.o > 0.5 && feed.some((l) => l.dieT < 0 && now < l.t0 + 0.25 + l.text.length * STEP_R);
  const cs = D().coordinator?.status;                       // the real session: working / blocked / done / idle
  const waiting = cs === 'blocked';
  const transient = now < coord.think;
  const st = speaking ? 'speak' : now < coord.deleg ? 'deleg' : (transient || cs === 'working' || conv.waiting) ? 'think' : waiting ? 'wait' : 'idle';
  if (st !== coord.state) { coord.state = st; boost(0.8); }
  const P = COORD_ST[st];
  coord.spd = ease(coord.spd, P[0], dt, 3);
  coord.rot += coord.spd * dt;
  let a = P[1];
  if (st === 'idle') a *= 0.8 + 0.2 * Math.sin(now * 1.1);
  if (st === 'speak') a *= 0.55 + 0.45 * Math.abs(Math.sin(now * 7.5));
  if (st === 'wait') a *= 0.55 + 0.45 * Math.pow(0.5 + 0.5 * Math.sin(now * 2.4), 3);
  // only a short event runs at the display rate; a long working session stays at the rest rate
  if ((st === 'think' && transient) || st === 'speak' || st === 'deleg') boost(0.05);
  coord.a = ease(coord.a, a, dt, 8);
  for (let k = 0; k < 3; k++) coord.col[k] = ease(coord.col[k], P[2][k], dt, 5);
  U.uCoord.value.set(-coord.rot * 0.7, coord.a, 0, 0);
  U.uCoordC.value.set(...coord.col);
  for (let i = 0; i < MAXA; i++) {
    const ag = agents[i];
    if (!ag) { U.uThr.value[i] = 0; continue; }
    ag.thr = Math.max(0, (ag.thr || 0) - dt / 1.6);
    U.uThr.value[i] = (ag.thr || 0) * (ag.thrDir || 1) || (ag.thrDir === -1 ? -0.0001 : 0.0001);
  }
}
function drawCoord(under) {
  const rx = L.coreX + 32, ry = L.coreY + 25, n = 64, c = coord.col;
  const chat = coord.state === 'think' ? 14 : coord.state === 'deleg' ? 8 : coord.state === 'speak' ? 5 : 0.6;
  for (let i = 0; i < n; i++) {
    const u = i / n + coord.rot / TAU;
    if ((u * 3 % 1 + 1) % 1 > 0.62) continue;
    const an = u * TAU, x = L.cx + Math.cos(an) * rx, y = L.cy + Math.sin(an) * ry;
    if (Math.abs(x - L.cx) < 26 && y < L.cy) continue;
    const edge = Math.sin(((u * 3 % 1 + 1) % 1) / 0.62 * Math.PI);
    glyph(x, y, 11, 18.9, alienAt(i * 7 + 3, Math.floor(now * chat + h1(i) * 5)), lerp(c[0], 1, 0.15), lerp(c[1], 1, 0.15), lerp(c[2], 1, 0.15), (0.5 + 0.5 * coord.a) * (0.35 + 0.65 * edge) * under(x, y));
  }
  const s = D().coordinator ? `COORDINATOR · ${String(D().coordinator.status || '').toUpperCase()}` : 'COORDINATOR · NOT IN HERDR';
  const f = 9.5, cw = f * 0.7, y0 = L.cy - L.coreY * 0.72, x0 = L.cx - (s.length - 1) * cw / 2;
  for (let i = 0; i < s.length; i++) glyph(x0 + i * cw, y0, cw, f * 1.2, latinIdx(s[i]), lerp(c[0], 1, 0.3), lerp(c[1], 1, 0.3), lerp(c[2], 1, 0.3), 0.55 + 0.3 * coord.a);
}

/* ---------- live data → the world ---------- */
const STATE_OF = (a) => (a.status === 'working' ? (a.stuck ? 'hung' : 'work') : a.status === 'blocked' ? 'wait' : 'done');
const secOfProject = (name) => sectors.findIndex((sc) => sc.p && sc.p.name === name);
let synced = false, lastReplyId = null;
function sync() {
  const s = D(), first = !synced;
  if (!s.projects) return;
  // sectors
  const inSec = (s.projects || []).filter((p) => p.sector != null && p.sector < 4);
  for (let i = 0; i < 4; i++) {
    const sc = sectors[i], p = inSec.find((x) => x.sector === i) || null;
    const look = p ? p.look : 'dormant', was = sc.look;
    sc.p = p; sc.letter = p ? shortTag(p.tag) : ''; sc.paused = !p || p.paused;
    sc.target = LOOK[look].slice();
    if (first) sc.w = sc.target.slice();
    else if (was && was !== look) {
      if (look === 'wounded') { sc.alarm = 1; Object.assign(perim, { r: 1, g: 0.06, b: 0.03, amt: 1, t0: now }); boost(2.6); }
      else if (was === 'wounded') ticker(`${sc.letter} · FIXED`, [0.4, 1, 0.66]);
      if (look === 'dormant') ticker(`${sc.letter} · PAUSED · TAKES NOTHING NEW`, [0.6, 0.8, 1]);
      if (was === 'dormant') ticker(`${sc.letter} · RESUMED`, [0.6, 0.8, 1]);
    }
    const rel = !!p?.release?.open;
    if (!first && rel && !sc.relOpen) { sc.wave = 0; sc.flare = 1; Object.assign(perim, { r: 1, g: 0.85, b: 0.3, amt: 1, t0: now }); ticker(`${sc.letter} · RELEASE WINDOW OPEN`, [1, 0.86, 0.36]); boost(2.5); }
    sc.relOpen = rel; sc.look = look;
    // trophies: what today's record closed for this project (done today, by its tag)
    const n = p ? (s.focus?.done_today || []).filter((x) => String(x).includes(p.tag)).length : 0;
    if (!first && n > sc.tally) sc.stampT = now + 0.2;
    sc.tally = Math.min(6, n);
  }
  // spawns: the live agents of the four projects
  const want = (s.agents || []).filter((a) => secOfProject(a.project) >= 0);
  const have = new Map(liveAgents().map((a) => [a.addr, a]));
  for (const d of want) {
    const sIdx = secOfProject(d.project), st = STATE_OF(d);
    let a = have.get(d.addr = d.address);
    if (!a) {
      a = spawn(sIdx, { addr: d.address, name: cut(clean(d.name).toUpperCase(), 22), life: first ? 1 : 0, heat: first ? 1 : 0, born: first ? now - 9 : now, tagT0: first ? now - 9 : now + 0.3 });
      if (!a) continue;
      if (!first) { a.thr = 1; a.thrDir = 1; coord.deleg = now + 2.2; ticker(`${SL(sIdx)} · ${a.name} STARTED`, secTextCol(sIdx)); }
    } else if (a.s !== sIdx) { a.s = sIdx; a.init = false; }
    a.d = d; have.delete(d.address);
    if (a.state !== st || first) {
      if (!first && a.state !== st) {
        if (st === 'done') { ticker(`${SL(sIdx)} · ${a.name} FINISHED`, [0.4, 1, 0.66]); threadPacket(a, false); }
        if (st === 'wait') ticker(`${SL(sIdx)} · ${a.name} WAITS FOR YOU`, [1, 0.75, 0.25]);
        if (st === 'hung') ticker(`${SL(sIdx)} · ${a.name} SILENT ${d.quiet} MIN`, [0.55, 0.7, 1]);
      }
      setAgentState(a, st);
    }
  }
  for (const a of have.values()) endAgent(a);
  // marks: problems and the operations that wait for the user (paused projects stay silent)
  const items = [];
  for (const p of s.projects || []) {
    const si = secOfProject(p.name);
    if (si < 0 || p.paused) continue;
    p.problems.forEach((x, i) => items.push({ key: `p:${p.name}:${x.text}`, kind: 'ci', s: si, problem: x, order: i,
      text: clean(x.text), label: x.data?.kind === 'pr_conflict' ? 'PR CONFLICT' : /^\s*🚫/u.test(x.text) ? 'BLOCKED' : 'PROBLEM', who: p.spoken }));
  }
  (s.ops || []).forEach((o, i) => {
    const si = secOfProject(o.project);
    if (si < 0 || o.paused || o.project_paused || !(o.group === 'you' || o.group === 'cut')) return;
    items.push({ key: `o:${o.id}`, kind: o.group === 'cut' ? 'hung' : 'ask', s: si, op: o, order: 100 + i,
      text: clean(o.ask || o.what), label: o.group === 'cut' ? 'INTERRUPTED' : (o.answers || []).length ? 'DECISION' : 'WAITS FOR YOU', who: o.agent || o.id });
  });
  const byKey = new Map(marks.filter((m) => m.burnT < 0).map((m) => [m.key, m]));
  const fresh = [];
  for (const it of items) {
    const until = dismissed.get(it.key);
    if (until && now < until) continue;
    let m = byKey.get(it.key);
    const ag = it.op?.agent ? liveAgents().find((a) => a.d?.name === it.op.agent) || null : null;
    if (m) { Object.assign(m, it, { agent: ag }); byKey.delete(it.key); continue; }
    m = { id: ++markNo, ...it, agent: ag, t0: now, burnT: -1, pending: !first };
    marks.push(m);
    if (!first) fresh.push(m);
  }
  for (const m of byKey.values()) { m.burnT = now; boost(1); }        // answered or closed elsewhere: burns out
  // a burst (a reconnect, a big change) lands quietly; one or two new marks arrive as shots
  if (fresh.length <= 2) fresh.forEach(notify); else { fresh.forEach((m) => { m.pending = false; }); ticker(`${fresh.length} NEW MARKS`, [1, 0.64, 0.1]); }
  // the coordinator's latest reply
  const r = s.reply;
  if (r && r.id !== lastReplyId) { const wasFirst = first || lastReplyId == null; lastReplyId = r.id; convArrive(r, wasFirst); }
  synced = true;
  hints(); boost(0.6); wake();
}

/* ---------- status lines (the data, read out on the field) ---------- */
const LOOK_WORD = { alive: 'ALIVE', wounded: 'WOUNDED', hunt: 'ON THE HUNT', dormant: 'PAUSED' };
function sectorLine(s) {
  const p = sectors[s].p, c = p.counts;
  return { sec: s, text: `${SL(s)} · ${LOOK_WORD[p.look]} · YOU ${c.you} · WORK ${c.work} · AGENTS ${liveAgents().filter((a) => a.s === s).length}` };
}
function sectorStatus(s) {
  const p = sectors[s].p;
  if (!p) return { lines: [{ sec: -1, text: 'NO PROJECT IN THIS SECTOR' }] };
  const lines = [sectorLine(s)];
  if (p.info.length) lines.push({ sec: s, text: `${SL(s)} · ${clean(p.info.join(' · '))}` });
  for (const u of p.urls) lines.push({ sec: s, text: `${SL(s)} · ${clean(u.name.split(' · ').pop())} · ${u.ok ? 'UP' : 'DOWN'} · ${u.ms} MS` });
  if (p.release) lines.push({ sec: s, text: `${SL(s)} · RELEASE ${p.release.open ? 'OPEN' : clean(p.release.text)}` });
  for (const x of p.problems.slice(0, 2)) lines.push({ sec: s, text: `${SL(s)} · ${clean(x.text)}` });
  return { lines: lines.slice(0, L.maxRows) };
}
function agentLine(a) {
  const d = a.d || {};
  return `${SL(a.s)} · ${a.name} · ${String(d.status || '').toUpperCase()}${d.model ? ` · ${clean(d.model.split(':')[0])}` : ''}${d.quiet != null ? ` · QUIET ${d.quiet} MIN` : ''}${d.goal ? ` · ${clean(d.goal)}` : ''}`;
}
function focusReply() {
  const f = D().focus || {}, mine = f.date === today(), lines = [];
  if (mine && f.focus) lines.push({ sec: -1, text: `FOCUS · ${clean(f.focus)}` });
  if (mine) (f.steps || []).forEach((st, i) => lines.push({ sec: -1, text: `${i + 1} · ${clean(st.text)}${st.done ? ' · DONE' : st.now ? ' · NOW' : ''}` }));
  (f.in_work || []).forEach((x) => lines.push({ sec: -1, text: `IN WORK · ${clean(typeof x === 'string' ? x : x.text)}` }));
  if (!lines.length) lines.push({ sec: -1, text: 'NO FOCUS OF THE DAY YET' });
  return { lines: lines.slice(0, L.maxRows) };
}
const today = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`; };
// the coordinator's reply: plain lines (markdown marks and code fences dropped), as many as the panel holds
function replyOf(r) {
  const w = W2[D().lang] || W2.en;
  const raw = String(r.text || '').replace(/```[\s\S]*?```/g, ' ').split('\n')
    .map((l) => clean(l.replace(/[*_`#>|]/g, ' ').replace(/^\s*[-•]\s+/, '· '))).filter(Boolean);
  const lines = raw.slice(0, 12).map((t, i) => ({ sec: -1, text: i ? t : `${w.reply} ${String(r.at || '').slice(11, 16)} · ${t}` }));
  return { lines, more: w.full };
}

/* ---------- channel (the coordinator's terminal) ---------- */
const chan = { o: 0, oT: 0, lastUse: -99, auto: false, touched: false };
function openChan(focus = true, isAuto = false) {
  if (chan.oT !== 1) { chan.auto = isAuto; chan.touched = false; }
  if (!isAuto) chan.touched = true;
  chan.oT = 1; chan.lastUse = now; boost(0.7);
  if (!isAuto) convOnOpen();
  if (focus) focusLine();                                  // at once: the next key already lands in the line
  hints(); wake();
}
function closeChan() { if (chan.oT === 0) return; chan.oT = 0; chan.byReply = false; conv.idx = 0; if (document.activeElement === cmdEl) cmdEl.blur(); boost(0.7); hints(); wake(); }
const chanOpen = () => chan.oT === 1;

const inp = { focused: false, text: '', born: [], target: null, tgtT: -9, lastKey: -9, focusA: 0 };
const IN_FONT = 18, LAB_FONT = 14;
let inputHotUntil = 0, corePulse = 0;
const jobs = [];
const later = (d, fn) => { jobs.push({ at: now + d, fn }); };
const kick = (sec = 0.5) => { inputHotUntil = Math.max(inputHotUntil, now + sec); chan.lastUse = now; wake(); };

function focusLine() { if (document.activeElement !== cmdEl) cmdEl.focus({ preventScroll: true }); }
// Focus with the channel closed is not the human: WebKit gives the old focus back to the line when the
// window returns (the GeniusConsole app on ⌥Space). It must not open the empty panel: the line lets go.
// The channel opens only on ` / Enter, a click on the handle, a reply, or a paste (F5 dictation, below).
cmdEl.addEventListener('focus', () => {
  if (!chanOpen()) { cmdEl.blur(); return; }
  inp.focused = true; chan.touched = true; hints(); kick(0.4);
});
// ⌘V while the line is closed (F5 dictation pastes with ⌘V, then presses Enter): open the channel and put
// the text into the line. «beforepaste» tells WebKit that the page takes the paste, so ⌘V is not disabled.
document.addEventListener('beforepaste', (e) => { if (shown && e.target !== cmdEl) e.preventDefault(); });
document.addEventListener('paste', (e) => {
  if (!shown || e.target === cmdEl) return;
  const text = (e.clipboardData && e.clipboardData.getData('text/plain')) || '';
  if (!text.trim()) return;
  e.preventDefault();
  openChan(false);
  cmdEl.focus({ preventScroll: true });
  cmdEl.value = cmdEl.value ? cmdEl.value + text : text.replace(/^\s+/, '');
  syncInput();
  kick(0.8);
});
cmdEl.addEventListener('blur', () => { inp.focused = false; hints(); kick(0.4); });

// «CH:» at the start aims the line at a sector (its tag), like the mock's «A:»…«D:»
const PREFIX = /^\s*([A-Za-z0-9]{1,5})\s*[:：]\s?/;
function syncInput(stagger) {
  let v = cmdEl.value;
  const m = v.match(PREFIX);
  if (m) {
    const s = sectors.findIndex((sc) => sc.p && sc.letter.replace(/\s/g, '').toLowerCase() === m[1].toLowerCase());
    if (s >= 0) {
      setLock({ kind: 'sector', s });
      v = v.slice(m[0].length); cmdEl.value = v; cmdEl.setSelectionRange(v.length, v.length);
      inp.text = v; inp.born = Array.from({ length: v.length }, (_, k) => now + (stagger || 0) * k);
      inp.lastKey = now; kick(0.6 + v.length * (stagger || 0)); return;
    }
  }
  const old = inp.text;
  let a = 0; while (a < old.length && a < v.length && old[a] === v[a]) a++;
  let b = 0; while (b < old.length - a && b < v.length - a && old[old.length - 1 - b] === v[v.length - 1 - b]) b++;
  const add = v.length - a - b, st = stagger ?? (add > 1 ? 0.012 : 0);
  const nb = inp.born.slice(0, a);
  for (let k = 0; k < add; k++) nb.push(now + k * st);
  inp.born = nb.concat(inp.born.slice(old.length - b));
  inp.text = v; inp.lastKey = now;
  kick(0.6 + add * st);
}
cmdEl.addEventListener('input', () => syncInput());
cmdEl.addEventListener('keyup', () => kick(0.3));
cmdEl.addEventListener('paste', () => kick(0.8));
cmdEl.addEventListener('keydown', (e) => {
  if (e.isComposing) return;
  if (e.altKey && e.code === 'KeyV') { e.preventDefault(); toggleVoice(); return; }
  if (e.key === 'Enter') { e.preventDefault(); send(); return; }
  if (e.key === 'PageUp' || e.key === 'PageDown') { e.preventDefault(); convScroll(e.key === 'PageUp' ? -1 : 1); return; }
  if (e.key === 'Escape') { e.preventDefault(); if (cmdEl.value) clearLine(); else closeChan(); return; }
  if (e.key === 'ArrowUp') { e.preventDefault(); recall(); return; }
  if (e.key === 'Backspace' && !cmdEl.value && lk) { e.preventDefault(); setLock(null); return; }
  kick(0.5);
});

function flashKeys() { elKeys.classList.add('warn'); setTimeout(() => elKeys.classList.remove('warn'), 500); }
function hints() {
  const k = (s) => `<b>${s}</b>`;
  let a, b;
  if (inp.focused) { a = `CHANNEL TO THE COORDINATOR · ${k('ENTER')} SEND · ${k('ESC')} CLEAR, AGAIN CLOSE`; b = `${k('CH:')} AIM AT A PROJECT · ${k('⌫')} ON EMPTY UNAIM · ${k('F5')} DICTATE · ${k('↑')} RECALL`; }
  else if (armed) { a = `${k(String(armed.k))} AGAIN TO CONFIRM ${actionsFor(lk)?.[armed.k - 1]?.label || ''}`; b = `${k('ESC')} CANCEL`; }
  else {
    a = `${k('1')} ${k('2')} ${k('3')} FIRE AT THE LOCK · ${k('L')} LATER · ${k('N')} NEXT MARK · ${k('CLICK')} LOCK · ${k('ESC')} RELEASE`;
    b = `${k('`')} / ${k('/')} CHANNEL · ${k('H')} RECALL · ${k('K')} SKIN`;
  }
  elKeys.innerHTML = `<div>${a}</div><div>${b}</div>`;
}

/* --- voice: the real dictation is F5 (it pastes into the focused line); nothing is simulated here --- */
const voice = { phase: 0 };
function toggleVoice() { openChan(true); ticker('PRESS F5 · DICTATION PASTES INTO THE LINE', [0.6, 0.8, 1]); }

/* --- packets: a sent line encodes into glyphs and flies to its target --- */
const packets = [];
function launch(chars, dest, onArrive, delay = 0) {
  if (!chars.length) { if (onArrive) onArrive(); return; }
  const p = { chars: chars.slice(0, 80), dest, onArrive, t0: now + delay, hit: 0 };
  packets.push(p);
  boost(delay + (dest ? 0.3 + p.chars.length * 0.014 + 0.85 : 0.8));
}
function drawPackets() {
  const tick2 = Math.floor(now * 20);
  for (let pi = packets.length - 1; pi >= 0; pi--) {
    const p = packets[pi], n = p.chars.length, age = now - p.t0;
    if (age < 0) continue;
    let alive = false;
    const dd = p.dest ? p.dest() : null;
    for (let k = 0; k < n; k++) {
      const c = p.chars[k], enc = (n - 1 - k) * 0.006;
      if (!dd) {
        const u = clamp((age - enc) / 0.45, 0, 1); if (u >= 1) continue; alive = true;
        if (age < enc) glyph(c.x, c.y, c.w, c.h, latinIdx(c.c), 1, 0.93, 0.78, 1);
        else glyph(c.x, c.y + u * 16, c.w * 0.9, c.h * 0.9, alienAt(k * 5 + 1, tick2), 0.55, 0.3, 0.9, 1 - u);
        continue;
      }
      const ts = 0.24 + k * 0.014, u = clamp((age - ts) / 0.72, 0, 1);
      if (u >= 1) { if (!p.hit) { p.hit = 1; if (p.onArrive) p.onArrive(); } continue; }
      alive = true;
      if (age < enc) { if (c.c !== ' ') glyph(c.x, c.y, c.w, c.h, latinIdx(c.c), 1, 0.93, 0.78, 1); continue; }
      const dx = dd[0], dy = dd[1], cpx = c.x, cpy = dy + (c.y - dy) * 0.35;
      for (let g2 = 0; g2 < 5; g2++) {
        const e0 = clamp(u - g2 * 0.03, 0, 1), e = e0 * e0 * (3 - 2 * e0), ie = 1 - e;
        const x = ie * ie * c.x + 2 * ie * e * cpx + e * e * dx, y = ie * ie * c.y + 2 * ie * e * cpy + e * e * dy;
        heatCol(g2 ? 0.72 - g2 * 0.08 : 0.97, tmp);
        const sz = lerp(1.15, 0.6, e) * (1 - g2 * 0.12), a = (u > 0.88 ? (1 - u) / 0.12 : 1) * (1 - g2 * 0.19);
        glyph(x, y, c.w * sz, c.h * sz, alienAt(k * 3 + g2, tick2 + k), tmp[0], tmp[1], tmp[2], a);
      }
    }
    if (!alive && age > 0.1) packets.splice(pi, 1);
  }
}

/* --- input layer (inside the open channel) --- */
function secTextCol(s) {
  const sc = sectors[s]; let r = 0, g = 0, b = 0;
  const w = sc.paused ? [0.6, 0, 0, 0.4] : sc.w;
  for (let k = 0; k < 4; k++) { r += w[k] * SEC_COL[k][0]; g += w[k] * SEC_COL[k][1]; b += w[k] * SEC_COL[k][2]; }
  const m = Math.max(r, g, b, 0.01), k2 = 0.95 / m;
  return [lerp(r * k2, 1, 0.25), lerp(g * k2, 1, 0.25), lerp(b * k2, 1, 0.25)];
}
function aimLabel() {
  if (!lk || lk.kind === 'core') return { text: 'COORDINATOR ▸', col: [0.55, 0.78, 1] };
  const s = lockSec(lk), who = lk.kind === 'agent' ? lk.a.name : lk.kind === 'mark' && lk.m.agent ? lk.m.agent.name : null;
  return { text: `${SL(s)}${who ? ' ' + cut(who, 14) : ''} ▸`, col: secTextCol(s) };
}
function inputLayout() {
  const cw = IN_FONT * 0.7, ch = IN_FONT * 1.2, lcw = LAB_FONT * 0.7, lch = LAB_FONT * 1.2;
  const al = aimLabel(), label = al.text;
  const lx = L.chL + 34;
  const x0 = lx + label.length * lcw + 10;
  const maxC = Math.max(4, Math.floor((L.chR - 28 - x0) / cw));
  const n = inp.text.length;
  const caret = inp.focused ? clamp(cmdEl.selectionStart ?? n, 0, n) : n;
  const start = Math.max(0, caret - maxC + 1);
  return { cw, ch, lcw, lch, label, lcol: al.col, lx, x0, maxC, caret, start };
}
function drawInput(ca) {
  const lay = inputLayout(), y = L.inY, { cw, ch } = lay;
  const tc = lay.lcol, fresh = now - inp.tgtT < 0.3;
  for (let i = 0; i < lay.label.length; i++) {
    const c = lay.label[i]; if (c === ' ') continue;
    glyph(lay.lx + i * lay.lcw, y, lay.lcw, lay.lch, fresh ? alienAt(i + 7, Math.floor(now * 30)) : latinIdx(c), tc[0], tc[1], tc[2], 0.95 * ca);
  }
  const t = inp.text, n = t.length, dim = (inp.focused ? 1 : 0.62) * ca;
  if (!n) {
    const ph = inp.focused ? 'TYPE · OR F5 TO DICTATE' : 'CLICK OR / TO TYPE';
    for (let i = 0; i < ph.length; i++) if (ph[i] !== ' ') glyph(lay.x0 + 6 + i * lay.lcw, y, lay.lcw, lay.lch, latinIdx(ph[i]), HUD[0], HUD[1], HUD[2], 0.85 * ca);
  }
  for (let i = lay.start; i < n && i < lay.start + lay.maxC; i++) {
    const c = t[i]; if (c === ' ') continue;
    const age = now - inp.born[i]; if (age < 0) continue;
    const x = lay.x0 + (i - lay.start) * cw;
    if (age < 0.13) glyph(x, y, cw * 0.95, ch * 0.95, alienAt(i * 7 + 3, Math.floor(now * 30)), 1, 0.55 + age * 3, 0.12, dim);
    else { const fl = clamp(1 - (age - 0.13) / 0.25, 0, 1); glyph(x, y, cw, ch, latinIdx(c), 1, lerp(0.93, 1, fl), lerp(0.78, 1, fl), dim); }
  }
  if (lay.start > 0) glyph(lay.x0 - cw * 0.9, y, cw, ch, latinIdx('‹'), 1, 0.7, 0.2, 0.8 * ca);
  if (inp.focused) {
    const on = now - inp.lastKey < 0.6 || Math.floor(now * 2.2) % 2 === 0;
    glyph(lay.x0 + (lay.caret - lay.start) * cw - cw * 0.5, y, 2.4, ch * 0.9, SOLID, 1, 0.72, 0.18, on ? ca : 0);
  }
}
function visibleChars() {
  const lay = inputLayout(), out = [];
  for (let i = lay.start; i < inp.text.length && i < lay.start + lay.maxC; i++)
    if (inp.text[i] !== ' ') out.push({ c: inp.text[i], x: lay.x0 + (i - lay.start) * lay.cw, y: L.inY, w: lay.cw, h: lay.ch });
  return out;
}
function emptyLine() { cmdEl.value = ''; inp.text = ''; inp.born = []; kick(0.4); }
function clearLine() { launch(visibleChars(), null); emptyLine(); }
// Every line goes to the coordinator. Aimed at a project or an agent, it starts with the project's
// tag (the house rule: «a message about a project starts with its tag») and the agent's name.
function send() {
  const text = cmdEl.value.trim();
  if (!text) { kick(0.3); return; }
  const s = lk && lk.kind !== 'core' ? lockSec(lk) : null;
  const p = s != null ? sectors[s].p : null;
  const who = lk?.kind === 'agent' ? lk.a.d?.name : lk?.kind === 'mark' ? lk.m.agent?.d?.name : null;
  const msg = p ? `${p.tag} · ${who ? who + ': ' : ''}${text}` : text;
  conv.sent = { text: msg, at: Date.now() }; conv.idx = 0;
  conv.waiting = { since: Date.now(), text: msg, doing: null };
  convRender(false);
  launch(visibleChars(), () => [L.cx, L.cy], () => {
    corePulse = 1; coordThink(1.4);
    call(api.send(msg), 'SENT TO THE COORDINATOR · THE REPLY COMES HERE');
  });
  emptyLine();
}

/* --- reply block (inside the channel) --- */
const feed = [], history = [];

/* --- the conversation (inside the open channel): the coordinator's latest reply in full, as HTML ---
   The canvas glyphs hold only Latin letters, so the reply (Ukrainian, markdown) is a DOM block over the
   panel: readable, scrollable (wheel, PgUp/PgDn), the owner's message above it, the 🔊 line apart.
   A new reply opens the channel, decodes once, and stays until Esc; it counts as read after 5 s on
   screen. While it is unread and the channel is closed, a teaser sits above the handle and a dot
   pulses on the core. H / ↑ walk back through earlier replies (GET /api/replies). */
const convEl = document.getElementById('conv'), teaserEl = document.getElementById('teaser'), dotEl = document.getElementById('unread');
const READ_KEY = 'genius.console.readReply';
const conv = { list: [], idx: 0, shownId: null, unread: null, seen: 0, sent: null, decodeEnd: 0, visible: false, box: '' };
try { conv.read = localStorage.getItem(READ_KEY); } catch { conv.read = null; }
const escH = (t) => String(t ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const hhmm = (at) => String(at || '').slice(11, 16);
function inlineMd(t) {
  const parts = String(t).split(/(`[^`]+`)/g);
  return parts.map((p) => (p.length > 1 && p.startsWith('`') && p.endsWith('`') ? `<code>${escH(p.slice(1, -1))}</code>`
    : escH(p).replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>').replace(/(^|[^*])\*([^*\s][^*]*)\*/g, '$1<i>$2</i>')
      .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<u>$1</u>'))).join('');
}
// markdown, simply: headings, bold, lists, tables as aligned rows, code as mono; the 🔊 line apart
function mdHtml(text) {
  const lines = String(text || '').replace(/\r/g, '').split('\n');
  const out = []; let spoken = '';
  for (let i = 0; i < lines.length; i++) {
    const ln = lines[i];
    if (/^\s*```/.test(ln)) {
      const code = []; i++;
      while (i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i++]);
      out.push(`<pre>${escH(code.join('\n'))}</pre>`); continue;
    }
    if (/^\s*🔊/.test(ln)) { spoken = ln.replace(/^\s*🔊\s*/, ''); continue; }
    if (/^\s*\|.*\|\s*$/.test(ln)) {
      const rows = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(lines[i++]);
      i--;
      const cells = rows.filter((r) => !/^\s*\|[\s:|-]+\|\s*$/.test(r)).map((r) => r.trim().slice(1, -1).split('|').map((c) => c.trim()));
      out.push(`<table>${cells.map((r, k) => `<tr>${r.map((c) => `<${k ? 'td' : 'th'}>${inlineMd(c)}</${k ? 'td' : 'th'}>`).join('')}</tr>`).join('')}</table>`);
      continue;
    }
    let m;
    if ((m = ln.match(/^\s*#{1,6}\s+(.*)$/))) { out.push(`<div class="h">${inlineMd(m[1])}</div>`); continue; }
    if ((m = ln.match(/^(\s*)([-*•]|\d+[.)])\s+(.*)$/))) {
      out.push(`<div class="li" style="padding-left:${1.4 + Math.min(3, Math.floor(m[1].length / 2)) * 1.2}em"><span class="bul">${/\d/.test(m[2]) ? escH(m[2]) : '·'}</span>${inlineMd(m[3])}</div>`);
      continue;
    }
    if (/^\s*>\s?/.test(ln)) { out.push(`<div class="q">${inlineMd(ln.replace(/^\s*>\s?/, ''))}</div>`); continue; }
    if (!ln.trim()) { out.push('<div class="gap"></div>'); continue; }
    out.push(`<div class="p">${inlineMd(ln)}</div>`);
  }
  return { html: out.join(''), spoken };
}
const plain = (t) => String(t || '').replace(/```[\s\S]*?```/g, ' ').replace(/[*_`#>|]/g, ' ').replace(/\s+/g, ' ').trim();
function teaserText(r) {
  const sp = String(r.text || '').split('\n').find((l) => /^\s*🔊/.test(l));
  if (sp) return sp.trim();
  const p = plain(r.text);
  const m = p.match(/^.{8,}?[.!?…](\s|$)/);
  return m ? m[0].trim() : p.slice(0, 160);
}
function convMerge(items) {
  const byId = new Map(conv.list.map((x) => [x.id, x]));
  for (const x of items || []) if (x && x.id) byId.set(x.id, Object.assign(byId.get(x.id) || {}, x));
  conv.list = [...byId.values()].sort((a, b) => String(a.at).localeCompare(String(b.at))).slice(-40);
}
async function convLoad() {
  try { const r = await fetch('/api/replies?limit=20'); if (r.ok) convMerge((await r.json()).replies); } catch { /* the bridge is older: only the latest reply */ }
}
const convLatest = () => conv.list[conv.list.length - 1] || null;
// The «thinking» loader: from the owner's send until a new reply (10 min at most, or the coordinator's
// turn ends): the owner's message, then COORDINATOR · THINKING · <s>, a glyph stream and the live
// activity line the bridge reads from the coordinator's screen (coordinator_activity).
const THINK_MAX = 600;
function convThinkHtml() {
  const you = D().lang === 'uk' ? 'ВИ' : 'YOU';
  return `<div class="c-ask"><span class="c-who">${you} · ${new Date(conv.waiting.since).toTimeString().slice(0, 5)}</span><div class="c-ask-t">${escH(conv.waiting.text)}</div></div>`
    + `<div class="c-think"><span class="c-who">COORDINATOR · THINKING · <span class="c-secs">0 S</span></span>`
    + '<div class="c-stream"></div><div class="c-doing"></div></div>';
}
function convThinkTick() {
  const w = conv.waiting;
  if (!w) return;
  const secs = Math.floor((Date.now() - w.since) / 1000);
  const act = D().coordinator_activity || {};
  // the end without a reply: 10 min, or the coordinator's turn is over (seen idle 8 s after the send)
  if (secs > THINK_MAX || (act.at && act.at * 1000 > w.since + 8000 && act.busy === false)) { conv.waiting = null; if (chanOpen()) convRender(false); return; }
  if (!chanOpen() || !convEl.querySelector('.c-think')) return;
  convEl.querySelector('.c-secs').textContent = `${secs} S`;
  let line = '';
  for (let i = 0; i < 64; i++) line += (i + Math.floor(Date.now() / 90)) % 9 < 6 ? NOISE[(Math.random() * NOISE.length) | 0] : ' ';
  convEl.querySelector('.c-stream').textContent = line;
  const doing = act.busy ? (act.doing || act.spin || '') : '';
  const el = convEl.querySelector('.c-doing');
  if (doing !== w.doing) { w.doing = doing; el.textContent = doing ? `▸ ${doing}` : ''; if (doing) convDecode(el); }
  if (now - (w.lastPacket || -9) > 2.2) {                  // a few glyphs fly from the core into the channel
    w.lastPacket = now;
    const pts = [];
    for (let k = 0; k < 5; k++) pts.push({ c: ' ', x: L.cx + (k - 2) * 7, y: L.cy + L.coreY * 0.6, w: 10, h: 17 });
    launch(pts, () => [L.cx, L.chTop + 12], null);
  }
}
setInterval(() => { if (shown && conv.waiting) convThinkTick(); }, 250);

function convRender(decode) {
  if (conv.waiting && conv.idx === 0) { convEl.innerHTML = convThinkHtml(); conv.shownId = null; convEl.scrollTop = 0; convThinkTick(); return; }
  const r = conv.list[conv.list.length - 1 - conv.idx];
  if (!r) { convEl.innerHTML = `<div class="c-empty">${escH(D().lang === 'uk' ? 'Відповідей координатора сьогодні ще немає' : 'No reply from the coordinator yet')}</div>`; conv.shownId = null; return; }
  const md = mdHtml(r.text);
  const sentNewer = conv.idx === 0 && conv.sent && (!r.ask || conv.sent.at > Date.parse(r.at || 0));
  const ask = sentNewer ? { text: conv.sent.text, wait: true } : r.ask;
  const you = D().lang === 'uk' ? 'ВИ' : 'YOU';
  const head = conv.idx ? ` · RECALL ${conv.idx + 1} OF ${conv.list.length}` : conv.unread === r.id ? ' · NEW' : '';
  // the owner's message: at most two lines (a click shows it all); the reply gets the rest
  convEl.innerHTML = (ask ? `<div class="c-ask" title="click: the whole message"><span class="c-who">${you}${ask.at ? ' · ' + hhmm(ask.at) : ''}${ask.wait ? ' · …' : ''}</span><div class="c-ask-t">${escH(String(ask.text).trim().slice(0, 2000))}</div></div>` : '')
    + `<div class="c-rep"><span class="c-who">COORDINATOR · ${hhmm(r.at)}${head}</span><div class="md">${md.html}</div>`
    + (md.spoken ? `<div class="c-spoken">🔊 ${inlineMd(md.spoken)}</div>` : '') + '</div>';
  conv.shownId = r.id;
  // opened at the top: the owner's two lines, then the start of the reply
  const rep = convEl.querySelector('.c-rep');
  convEl.scrollTop = 0;
  const askEl = convEl.querySelector('.c-ask');
  if (askEl) askEl.addEventListener('click', () => askEl.classList.toggle('full'));
  if (decode && !document.hidden) convDecode(rep);
}
// the decode effect: only on arrival, ~1 s, then plain text
const NOISE = 'ЖЯЮЄЇФЦШЩ#%&*+=<>/\\0123456789ABCDEFXZ';
function convDecode(root) {
  const nodes = [];
  const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let n = w.nextNode(); n; n = w.nextNode()) if (n.nodeValue.trim()) nodes.push([n, n.nodeValue]);
  const total = nodes.reduce((a, [, t]) => a + t.length, 0) || 1, dur = Math.min(1.4, 0.45 + total * 0.0009);
  const t0 = performance.now(); conv.decodeEnd = t0 + dur * 1000;
  const step = () => {
    const k = Math.min(1, (performance.now() - t0) / (dur * 1000));
    let pos = 0; const edge = k * total * 1.15;
    for (const [n, t] of nodes) {
      let s = '';
      for (let i = 0; i < t.length; i++) { const g = pos + i; s += g < edge - 12 || t[i] === ' ' || t[i] === '\n' ? t[i] : g < edge ? NOISE[(Math.random() * NOISE.length) | 0] : ' '; }
      n.nodeValue = s; pos += t.length;
    }
    if (k < 1 && !document.hidden) requestAnimationFrame(step); else for (const [n, t] of nodes) n.nodeValue = t;
  };
  requestAnimationFrame(step);
}
// a reply in the SSE state: the latest one
async function convArrive(r, first) {
  if (!r || !r.id) return;
  convMerge([r]);
  await convLoad();                      // its question, and the earlier replies for H / ↑
  const fresh = r.id !== conv.read;
  conv.unread = fresh ? r.id : null; conv.seen = 0;
  if (!first) { conv.sent = null; conv.waiting = null; }
  if (fresh && !first) {
    conv.idx = 0;
    if (!chanOpen()) openChan(false, true);
    chan.byReply = true; chan.lastUse = now;
    convRender(true);
    corePulse = 1;
  } else if (chanOpen() && conv.idx === 0) convRender(false);
  convChrome();
}
function convOnOpen() { if (conv.idx === 0 || !conv.shownId) { conv.idx = 0; convRender(false); } }
function convRecall() {
  if (!conv.list.length) { convLoad().then(() => { if (conv.list.length) convRecall(); }); if (!chanOpen()) openChan(false); return; }
  if (!chanOpen()) { conv.idx = 0; openChan(false); convRender(false); return; }
  conv.idx = (conv.idx + 1) % conv.list.length;
  convRender(false);
  chan.touched = true; chan.lastUse = now;
}
function convScroll(dir) { convEl.scrollBy({ top: dir * convEl.clientHeight * 0.85, behavior: 'smooth' }); chan.lastUse = now; }
// teaser over the handle and the dot on the core: an unread reply while the channel is closed
function convChrome() {
  const r = conv.unread && conv.list.find((x) => x.id === conv.unread);
  const on = !!r && !chanOpen() && shown;
  teaserEl.hidden = !on; dotEl.hidden = !on;
  if (on) teaserEl.innerHTML = `<span class="dot"></span>${escH(teaserText(r))}`;
}
// every frame: place the block over the open panel; count the seconds it is read
function convFrame(dt, top, hw) {
  const vis = chan.o > 0.86 && !feed.some((l) => l.dieT < 0) && shown;
  if (vis !== conv.visible) { conv.visible = vis; convEl.hidden = !vis; }
  if (vis) {
    const box = `${Math.round(L.cx - hw + 28)},${Math.round(top + 16)},${Math.round(hw * 2 - 56)},${Math.round(L.inY - 34 - (top + 16))}`;
    if (box !== conv.box) {
      conv.box = box; const [x, y, w, h] = box.split(',');
      Object.assign(convEl.style, { left: x + 'px', top: y + 'px', width: w + 'px', height: Math.max(40, +h) + 'px' });
    }
    convEl.style.opacity = String(clamp((chan.o - 0.86) / 0.14, 0, 1));
    if (conv.unread && conv.shownId === conv.unread && !document.hidden && performance.now() > conv.decodeEnd) {
      conv.seen += dt;
      if (conv.seen >= 5) {
        conv.read = conv.unread; conv.unread = null;
        try { localStorage.setItem(READ_KEY, conv.read); } catch { /* read this session only */ }
        const w = convEl.querySelector('.c-rep .c-who'); if (w) w.textContent = w.textContent.replace(' · NEW', '');
      }
    }
  }
  const tb = `${Math.round(L.cx)},${Math.round(H - 18 - L.handleH - 10)},${Math.round(L.cy - L.coreY)}`;
  if (tb !== conv.tb) {
    conv.tb = tb;
    teaserEl.style.left = L.cx + 'px'; teaserEl.style.bottom = (18 + L.handleH + 10) + 'px'; teaserEl.style.maxWidth = Math.round(L.chHW * 2) + 'px';
    dotEl.style.left = (L.cx + 30) + 'px'; dotEl.style.top = (L.cy - L.coreY - 12) + 'px';
  }
  convChrome();
}
teaserEl.addEventListener('click', () => { conv.idx = 0; openChan(true); });
// a click on the panel puts the keyboard into the line (not while the owner selects text to copy)
convEl.addEventListener('click', (e) => {
  if (e.target.closest('.c-ask')) return;
  const sel = window.getSelection && String(window.getSelection());
  if (!sel) openChan(true);
});
convEl.addEventListener('wheel', (e) => { e.stopPropagation(); chan.lastUse = now; }, { passive: true });
let feedLastT = -99, recallUntil = 0, recallIdx = -1, lineSeed = 0;
const STEP_R = 0.02;
function wrapText(s, max) {
  if (s.length <= max) return [s];
  const out = []; let cur = '';
  for (const w of s.split(' ')) {
    const lim = out.length ? max - 4 : max;
    if (cur && (cur + ' ' + w).length > lim) { out.push(cur); cur = w; } else cur = cur ? cur + ' ' + w : w;
    while (cur.length > lim) { out.push(cur.slice(0, lim)); cur = cur.slice(lim); }
  }
  if (cur) out.push(cur);
  return out;
}
function flashSector(s, k = 0.6) { const sc = sectors[s]; sc.flare = Math.max(sc.flare, k * 0.75); sc.after = Math.max(sc.after, k); boost(1); }
function killLine(l) { if (l.dieT < 0) { l.dieT = now; boost(1.3); } }
function killFeed() { feed.forEach(killLine); }
// A reply that arrives while the channel is closed opens it briefly (12 s, unless the human touches it).
function showReply(rep, opts = {}) {
  if (!rep.lines.length) return;
  if (!chanOpen()) openChan(false, true);
  chan.lastUse = now;
  killFeed();
  if (!opts.recall && !opts.reshow) { history.push(rep); if (history.length > 12) history.shift(); }
  const rows = [];
  if (opts.recall) rows.push({ kind: 'tag', sec: -1, text: opts.recall });
  rep.lines.forEach((ln) => wrapText(clean(ln.text), L.fMaxChars).forEach((s, j) => rows.push({ kind: ln.tag ? 'tag' : 'line', sec: j ? -2 : ln.sec, text: j ? '    ' + s : s })));
  // a long text keeps its first rows; the last row says where the rest is
  if (rows.length > L.maxRows) rows.splice(L.maxRows - 1, rows.length, { kind: 'tag', sec: -1, text: cut(clean(rep.more || '…'), L.fMaxChars) });
  let t = now + 0.3;
  for (const r of rows) {
    feed.push(Object.assign(r, { rep, t0: t, y: null, dieT: -1, seed: (lineSeed += 17), recall: !!opts.recall }));
    t += 0.16 + r.text.length * STEP_R * 0.55;
  }
  const end = t + 0.3;
  feedLastT = end;
  recallUntil = opts.recall ? end + 5 : 0;
  boost(end - now + 0.4);
  later(end - now, () => { chan.lastUse = now; hints(); });
}
function recall() { convRecall(); }
function recallFeed() {
  if (!history.length) { if (!chanOpen()) openChan(false); kick(0.2); return; }
  recallIdx = recallUntil && now < recallUntil ? (recallIdx + 1) % history.length : 0;
  showReply(history[history.length - 1 - recallIdx], { recall: `RECALL ${recallIdx + 1} OF ${history.length}` });
  chan.touched = true;
}
function updateChan(dt) {
  const o0 = chan.o;
  chan.o = ease(chan.o, chan.oT, dt, 9);
  if (Math.abs(chan.o - chan.oT) < 0.002) chan.o = chan.oT;
  if (chan.o !== o0) boost(0.1);
  // auto close: 30 s idle with an empty line; a reply that opened it by itself: 12 s (25 s for a long reply)
  // a reply that opened the channel stays until Esc; so does an unread one
  if (chanOpen() && !inp.text && !inp.focused && !chan.byReply && !conv.unread) {
    const lim = chan.auto && !chan.touched ? 25 : 30;
    if (now - chan.lastUse > lim) closeChan();
  }
  const o = chan.o;
  const hw = lerp(L.handleHW, L.chHW, smoothstep01(o / 0.45));
  const hgt = lerp(L.handleH, feed.some((l) => l.dieT < 0) ? L.panelH : L.convH, smoothstep01((o - 0.2) / 0.8));
  const top = H - 18 - hgt, bot = H + 40;
  L.chNowHW = hw; L.chTop = top;
  convFrame(dt, top, hw);
  U.uChan.value.set(L.cx, (top + bot) / 2, hw, (bot - top) / 2);
}
const smoothstep01 = (x) => { x = clamp(x, 0, 1); return x * x * (3 - 2 * x); };
function updateDialog(dt) {
  updateChan(dt);
  inp.focusA = ease(inp.focusA, inp.focused ? 1 : 0, dt, 10);
  const cc = lk && lk.kind !== 'core' ? secTextCol(lockSec(lk)) : inp.focused ? [1, 0.62, 0.12] : [0.42, 0.32, 0.8];
  const C = U.uChanC.value; C.set(ease(C.x, cc[0], dt, 9), ease(C.y, cc[1], dt, 9), ease(C.z, cc[2], dt, 9));
  U.uChanA.value = inp.focusA * 0.8 + 0.2;
  U.uChanV.value = 0;
  const alive = feed.filter((l) => l.dieT < 0);
  if (alive.length) {
    if (recallUntil && now > recallUntil) { recallUntil = 0; killFeed(); }
    else if (!recallUntil && now > feedLastT + 25) killFeed();
  }
}
function drawFeed(dt, ca) {
  const f = L.fFont, cw = f * 0.7, ch = f * 1.2, tick2 = Math.floor(now * 14);
  const coreX = L.cx, coreY = L.cy + L.coreY * 0.55;
  let idx = 0;
  for (const l of feed) { if (l.dieT >= 0) continue; const yT = L.fTop + L.fRow * (idx + 0.5); l.y = l.y == null ? yT : ease(l.y, yT, dt, 9); idx++; }
  for (let i = feed.length - 1; i >= 0; i--) if (feed[i].dieT >= 0 && now - feed[i].dieT > 1.25) feed.splice(i, 1);
  if (ca < 0.02) return;
  for (const l of feed) {
    const n = l.text.length, da = l.dieT >= 0 ? now - l.dieT : -1;
    if (now < l.t0 - 0.3) continue;
    const drift = da > 0 ? da * da * 70 + da * 18 : 0, fade = (da > 0 ? clamp(1 - (da - 0.25) / 0.9, 0, 1) : 1) * ca;
    if (l.sec >= 0 && !l.flashed && da < 0 && now >= l.t0 + 0.22) { l.flashed = 1; flashSector(l.sec); }
    const sc = l.sec >= 0 ? secTextCol(l.sec) : null;
    const lead = l.sec >= 0 ? SL(l.sec).length : 1;
    for (let i = 0; i < n; i++) {
      const c = l.text[i]; if (c === ' ') continue;
      const ta = l.t0 + i * 0.006, fly = 0.3, td = l.t0 + 0.22 + i * STEP_R;
      if (now < ta - fly) continue;
      const sx = L.fLeft + i * cw;
      let px = sx, py = l.y - drift, a = fade, sz = 1, alien = now < td;
      if (now < ta) { const u = (now - (ta - fly)) / fly, e = 1 - (1 - u) * (1 - u); px = lerp(coreX, sx, e); py = lerp(coreY, l.y, e); sz = 0.55 + 0.45 * e; a = u * ca; }
      if (da >= 0 && da > (n - 1 - i) * 0.006) alien = true;
      let col = [1, 0.97, 0.88];
      if (l.kind === 'tag') col = [1, 0.7, 0.22];
      else if (sc && i < lead) col = sc;
      else if (sc && c === '·' && i < lead + 2) col = [0.55, 0.58, 0.68];
      if (alien) { const hc = sc || [1, 0.6, 0.12]; glyph(px, py, cw * 0.86 * sz, ch * 0.86 * sz, alienAt(i + l.seed, tick2), hc[0], hc[1], hc[2], a * 0.9); }
      else { const fl = clamp(1 - (now - td) / 0.2, 0, 1); glyph(px, py, cw, ch, latinIdx(c), lerp(col[0], 1, fl), lerp(col[1], 1, fl), lerp(col[2], 1, fl), a); }
    }
  }
}

/* ---------- clicks ---------- */
const labRects = [];
function canvasHit(x, y) {
  for (const r of labRects) if (x >= r.x0 && x <= r.x1 && y >= r.y0 && y <= r.y1) return { fire: r.k };
  if (Math.abs(x - L.cx) <= L.chNowHW + 4 && y >= L.chTop - 4) return { chan: 1 };
  for (const g of groups().slice(0, MAXM)) { const p = markPos(g.top); if (Math.hypot(x - p[0], y - p[1]) < 26) return { mark: g.top }; }
  for (const a of liveAgents()) if (Math.hypot(x - a.x, y - a.y) < L.agentR + 12) return { agent: a };
  if (Math.abs(x - L.cx) / L.coreX + Math.abs(y - L.cy) / L.coreY < 1) return { core: 1 };
  const qx = x - L.cx, qy = y - L.cy;
  return { sec: Math.abs(qy) * W > Math.abs(qx) * H ? (qy < 0 ? 0 : 2) : (qx > 0 ? 1 : 3) };
}
canvas.addEventListener('mousedown', (e) => e.preventDefault());
canvas.addEventListener('click', (e) => {
  const h = canvasHit(e.clientX, e.clientY);
  if (h.fire) { fire(h.fire); return; }
  if (h.chan) { openChan(true); return; }
  if (h.mark) { setLock({ kind: 'mark', m: h.mark }); return; }
  if (h.agent) { setLock({ kind: 'agent', a: h.agent }); return; }
  if (h.core) { setLock({ kind: 'core' }); return; }
  if (lk && lk.kind === 'sector' && lk.s === h.sec) setLock(null); else setLock({ kind: 'sector', s: h.sec });
});

/* ---------- keys ---------- */
window.addEventListener('keydown', (e) => {
  if (!shown || e.target === cmdEl || e.metaKey || e.ctrlKey) return;
  const code = e.code || '';
  if (e.altKey) { if (code === 'KeyV') { e.preventDefault(); toggleVoice(); } return; }
  if (code === 'Backquote' || code === 'Slash' || e.key === '/' || e.key === 'Enter') { e.preventDefault(); openChan(true); return; }
  if (/^Digit[1-3]$/.test(code)) { e.preventDefault(); fire(+code.slice(5)); wake(); return; }
  if (code === 'KeyL') { e.preventDefault(); fireLater(); wake(); return; }
  if (code === 'KeyN') { e.preventDefault(); cycle(); wake(); return; }
  if (code === 'KeyV') { e.preventDefault(); toggleVoice(); return; }
  if (code === 'KeyH' || e.key === 'ArrowUp') { e.preventDefault(); recall(); return; }
  if ((e.key === 'PageUp' || e.key === 'PageDown') && chanOpen()) { e.preventDefault(); convScroll(e.key === 'PageUp' ? -1 : 1); return; }
  if (e.key === 'Escape') {
    if (armed) { armed = null; hints(); }
    else if (chanOpen()) closeChan();
    else if (lk) setLock(null);
    wake();
  }
});

/* ---------- per-frame update ---------- */
const heatCol = (h, out) => {
  h = clamp(h, 0, 1);
  const stops = [[0, 0.02, 0.02, 0.12], [0.22, 0.10, 0.09, 0.55], [0.45, 0.72, 0.08, 0.66], [0.66, 1, 0.42, 0.06], [0.84, 1, 0.88, 0.2], [1, 1, 1, 0.95]];
  for (let i = 1; i < stops.length; i++) if (h <= stops[i][0]) {
    const a = stops[i - 1], b = stops[i], k = (h - a[0]) / (b[0] - a[0]);
    out[0] = lerp(a[1], b[1], k); out[1] = lerp(a[2], b[2], k); out[2] = lerp(a[3], b[3], k); return out;
  }
  return out;
};
const tmp = [0, 0, 0];
const HUD = [0.40, 1, 0.20]; // #66ff33, acid phosphor green: every small utility label
const plate = (cx, cy, w, h, a) => glyph(cx, cy, w, h, SOLID, 0, 0, 0, a); // thin dark backing
const SEC_COL = [[0.52, 0.40, 1.0], [1, 0.10, 0.05], [1, 0.64, 0.10], [0.20, 0.25, 0.36]];
const hudText = (s, cx, y, f, a = 1) => { const cw = f * 0.7; for (let i = 0; i < s.length; i++) if (s[i] !== ' ') glyph(cx - (s.length - 1) * cw / 2 + i * cw, y, cw, f * 1.2, latinIdx(s[i]), HUD[0], HUD[1], HUD[2], a); };

// centre: the one thing that matters now — the locked mark, else the top waiting item, else the focus of the day
const foc = { key: '', t0: -9 };
function centreItem() {
  const open = openMarks().filter((m) => !m.pending);
  const m = lk && lk.kind === 'mark' && lk.m.burnT < 0 ? lk.m : open[0];
  if (m) {
    const i = open.indexOf(m);
    return { key: 'm' + m.id + m.text, kick: `${SL(m.s)} · ${m.label}${open.length > 1 ? ` · ${i + 1} OF ${open.length}` : ''}`, main: m.text, sub: clean(m.who || ''), col: KIND[m.kind].col };
  }
  const f = D().focus || {}, mine = f.date === today();
  if (mine && f.focus) {
    const steps = f.steps || [], n = steps.findIndex((st) => st.now);
    return { key: 'f' + f.focus + n, kick: 'FOCUS OF THE DAY', main: clean(f.focus), sub: n >= 0 ? `STEP ${n + 1} OF ${steps.length} · ${clean(steps[n].text)}` : `${steps.filter((s) => s.done).length} OF ${steps.length} DONE`, col: [0.55, 0.76, 1] };
  }
  const w = (f.in_work || [])[0];
  if (w) return { key: 'w' + (w.text || w), kick: 'IN WORK', main: clean(w.text || w), sub: clean(w.agent || ''), col: [0.55, 0.76, 1] };
  return { key: 'quiet', kick: 'ALL QUIET', main: D().projects ? 'NOTHING WAITS FOR YOU' : 'CONNECTING TO THE BRIDGE', sub: '', col: [0.55, 0.76, 1] };
}
function drawCentre() {
  const open = openMarks(), c = centreItem();
  if (c.key !== foc.key) { foc.key = c.key; foc.t0 = now; boost(1.6); }
  const age = now - foc.t0, t2 = Math.floor(now * 16);
  const glitchSlot = Math.floor(now / 2.3), glitchOn = now % 2.3 < 0.16;
  const row = (s, y, f, col, startI, dimRest) => {
    const cw = f * 0.7, ch = f * 1.2, x0 = L.cx - (s.length - 1) * cw / 2;
    const gi = Math.floor(h1(glitchSlot + startI) * s.length);
    for (let i = 0; i < s.length; i++) {
      const ch2 = s[i]; if (ch2 === ' ') continue;
      const td = 0.12 + (startI + i) * 0.016;
      if (age < (startI + i) * 0.004) continue;
      const alien = age < td || (glitchOn && i === gi && age > 3);
      if (alien) glyph(x0 + i * cw, y, cw * 0.86, ch * 0.86, alienAt(i + startI, t2), col[0], col[1], col[2], 0.9);
      else { const fl = clamp(1 - (age - td) / 0.2, 0, 1), cc = dimRest === 'sub' ? [0.55, 0.63, 0.78] : dimRest === 'kick' ? col : [1, 0.97, 0.9];
        glyph(x0 + i * cw, y, cw, ch, latinIdx(ch2), lerp(cc[0], 1, fl), lerp(cc[1], 1, fl), lerp(cc[2], 1, fl), 1); }
    }
  };
  // the main text wraps into up to three lines inside the plate
  const availW = 2 * L.coreX * 0.78 - 30;
  let mf = clamp(Math.floor(availW / (c.main.length * 0.7)), 12, 22), lines = [c.main];
  if (mf <= 12) { mf = 12.5; lines = wrapText(c.main, Math.floor(availW / (mf * 0.7))).map((s) => s.trim()); if (lines.length > 3) { lines = lines.slice(0, 3); lines[2] = cut(lines[2], lines[2].length) + '…'; } }
  const lh = mf * 1.25, y0 = L.cy + 1 - (lines.length - 1) * lh / 2;
  row(cut(c.kick, Math.floor(availW / 7.7)), L.cy - L.coreY * 0.5, 11, c.col, 0, 'kick');
  lines.forEach((s, j) => row(s, y0 + j * lh, mf, c.col, c.kick.length + j * 40, true));
  if (c.sub) row(cut(c.sub, Math.floor(availW / 7.7)), L.cy + L.coreY * 0.5, 11, c.col, c.kick.length + 130, 'sub');
  // badge: how many marks wait
  const n = open.length, hot = n > 0, bx = L.cx, by = L.cy - L.coreY - 2;
  U.uBadge.value.w = ease(U.uBadge.value.w, hot ? 1 : 0, 0.08, 6);
  const ns = String(n), bf = n > 99 ? 11 : 15;
  for (let i = 0; i < ns.length; i++) glyph(bx + (i - (ns.length - 1) / 2) * bf * 0.7, by + 1, bf * 0.7, bf * 1.2, latinIdx(ns[i]), hot ? 1 : 0.55, hot ? 0.85 : 0.7, hot ? 0.5 : 0.95, 1);
  // the hive count under the plate (agents of the other projects are counted, not drawn)
  const all = (D().agents || []).length, here = liveAgents().length;
  hudText(`${here} ACTIVE${all > here ? ` · +${all - here} ELSEWHERE` : ''}`, L.cx, L.cy + L.coreY * 0.8, 12);
  return age < 2.2;
}

function update(dt) {
  for (let i = jobs.length - 1; i >= 0; i--) if (now >= jobs[i].at) { const j = jobs[i]; jobs.splice(i, 1); j.fn(); }
  if (dirty) { dirty = false; sync(); }
  updateShot();
  updateFx();
  updateCoord(dt);
  updateDialog(dt);
  const t = now;

  sectors.forEach((s, i) => {
    for (let k = 0; k < 4; k++) s.w[k] = ease(s.w[k], s.target[k], dt, 3.2);
    s.speed = ease(s.speed, s.speedT, dt, 3);
    s.phase += dt * s.speed;
    s.alarm = Math.max(0, s.alarm - dt / 2.2);
    s.flare = Math.max(0, s.flare - dt / 3.2);
    if (s.wave >= 0) { s.wave += dt * 0.8; s.after = 1; if (s.wave > 1.4) s.wave = -1; }
    s.after = Math.max(0, s.after - dt / 4);
    U.uSec.value[i].set(s.w[0], s.w[1], s.w[2], s.w[3]);
    U.uSecFx.value[i].set(s.wave < 0 ? -9 : s.wave, s.alarm, s.phase, s.after);
  });

  // spawns
  placeAgents(dt);
  for (let i = agents.length - 1; i >= 0; i--) if (agents[i].dieT >= 0 && now - agents[i].dieT > 1.0) {
    const a = agents[i]; agents.splice(i, 1);
    marks.forEach((m) => { if (m.agent === a) m.agent = null; });
    if (lk && (lk.a === a)) setLock(null);
  }
  for (let i = marks.length - 1; i >= 0; i--) if (marks[i].burnT >= 0 && now - marks[i].burnT > 0.9) {
    if (lk && lk.m === marks[i]) setLock(null);
    marks.splice(i, 1);
  }
  for (let i = 0; i < MAXA; i++) {
    const a = agents[i];
    if (!a) { U.uAg.value[i].set(0, 0, 0, 0); U.uAgB.value[i].set(0, 0, 0, 0); continue; }
    if (!a.hold) a.life = a.dieT >= 0 ? 1 - clamp((now - a.dieT) / 0.9, 0, 1) : clamp((now - a.born) / 0.7, 0, 1);
    a.heat = ease(a.heat, a.heatT, dt, a.heatK);
    a.move = ease(a.move, a.moveT, dt, 2);
    a.red = ease(a.red, a.redT, dt, 5);
    a.flag = ease(a.flag, a.flagT, dt, 7);
    a.ph += dt * a.move;
    a.x = a.hx ?? L.cx; a.y = a.hy ?? L.cy;
    const born = a.dieT < 0 && !a.hold ? clamp(1 - (now - a.born) / 0.7, 0, 1) : 0, burn = a.dieT >= 0 ? clamp((now - a.dieT) / 0.9, 0, 1) : 0;
    const flare = Math.max(born * born, Math.sin(burn * Math.PI) * 0.9, clamp(1 - (now - a.reqT) / 0.8, 0, 1) * 0.8);
    const R = a.hold ? 0 : agentR(a) * (1 + flare * 0.6);
    U.uAg.value[i].set(a.x, a.y, a.hold ? 0 : clamp(a.heat * a.life + flare * 0.9, 0, 1.4), R < 0.6 ? 0 : R);
    U.uAgB.value[i].set(a.red, a.seed + a.ph * 0.8, a.flag * a.life, 0.08 + 0.3 * a.move);
  }

  // reticle
  let gx, gy, k = 1.2;
  const lp = lockPos(lk);
  if (shot && shot.st < 3) { gx = shot.hold[0]; gy = shot.hold[1]; k = 8; ret.rotV = 9; ret.spreadT = 40 + 8 * Math.sin(t * 14); ret.lockT = 0; ret.amberT = 0; }
  else if (lp) {
    gx = lp[0]; gy = lp[1]; k = 7;
    const sp0 = lk.kind === 'core' ? CORE_SPREAD() : 30;
    if (now - lk.t0 < 0.4) { ret.rotV = 7; ret.spreadT = sp0 + 2 + 12 * Math.sin(t * 14); ret.lockT = 0; }
    else {
      if (ret.lockT === 0) ret.fire = Math.max(ret.fire, 0.7);
      ret.rotV = 0; ret.lockT = 1; ret.amberT = armed ? 0 : 1; ret.spreadT = fx ? ret.spreadT : sp0;
      const per = TAU / 3, tr = ROT0 + per * Math.round((ret.rot - ROT0) / per);
      ret.rot = ease(ret.rot, tr, dt, 12);
    }
  } else { gx = W * (0.5 + 0.37 * Math.sin(t * 0.13 + 1.0)); gy = H * (0.5 + 0.34 * Math.sin(t * 0.19)); ret.rotV = 0.4; ret.spreadT = 22; ret.lockT = 0; ret.amberT = 0; }
  if (fx && now - fx.t0 > 0.25) ret.spreadT = lk && lk.kind === 'core' ? CORE_SPREAD() : 30;
  U.uRetK.value = ease(U.uRetK.value, lk && lk.kind === 'core' && !(shot && shot.st < 3) ? 0 : 1, dt, 10);
  ret.x = ease(ret.x, gx, dt, k); ret.y = ease(ret.y, gy, dt, k);
  ret.spread = ease(ret.spread, ret.spreadT, dt, 12);
  ret.lock = ease(ret.lock, ret.lockT, dt, 14);
  ret.amber = ease(ret.amber, ret.amberT, dt, 10);
  ret.rot += dt * ret.rotV;
  ret.fire = Math.max(0, ret.fire - dt * 2.4);
  const armPulse = armed ? 0.6 + 0.4 * Math.sin(t * 16) : 0;
  U.uRet.value.set(ret.x, ret.y, ret.spread, ret.rot);
  U.uRetB.value.set(ret.lock, ret.alpha + 0.25 * ret.lock + armPulse * 0.5, ret.amber, Math.max(ret.fire, armPulse * 0.5));
  if (armed) { boost(0.15); if (now - armed.t > 4) { armed = null; hints(); } }
  updateLasers();

  coreHot = ease(coreHot, openMarks().length ? 1 : 0, dt, 6);
  corePulse = Math.max(0, corePulse - dt * 1.6);
  U.uCoreHot.value = Math.max(coreHot, corePulse * 0.85);
  U.uTime.value = t % 3600;

  // marks to the shader: one per group (its top item), plus the ones burning out; a mark still in flight is not drawn
  const vis = marks.filter((m) => m.burnT >= 0).map((m) => ({ top: m, n: 1 })).concat(groups().filter((g) => !g.top.pending)).slice(0, MAXM);
  for (let i = 0; i < MAXM; i++) {
    const g = vis[i];
    if (!g) { U.uMk.value[i].w = 0; continue; }
    const m = g.top, p = markPos(m), K = KIND[m.kind], bA = now - m.t0;
    const slam = clamp(1 - bA / 0.3, 0, 1), b = m.burnT >= 0 ? clamp((now - m.burnT) / 0.9, 0, 1) : 0;
    U.uMk.value[i].set(p[0], p[1], 18 + slam * 26 + b * 26, clamp(bA / 0.15, 0, 1) * (1 - b));
    U.uMkC.value[i].set(lerp(K.col[0], 1, b), lerp(K.col[1], 1, b), lerp(K.col[2], 1, b), b > 0 ? (1 - b) * (1 - b) : slam * 0.8);
    g._p = p;
  }

  // ---- glyphs ----
  gN = 0;
  const tickOn = !!tick;
  const inPanel = (x, y) => chan.o > 0.05 && Math.abs(x - L.cx) < L.chNowHW + 10 && y > L.chTop - 10;
  const under = (x, y) => {
    if (inPanel(x, y)) return 0;
    if (tickOn && Math.abs(y - 50) < 26 && Math.abs(x - U.uBand.value.x) < U.uBand.value.z + 30) return 1 - bandA;
    return 1;
  };

  // frame: a stream that crawls around the window; it parts at the hatch
  const inset = 18, pw = W - inset * 2, ph = H - inset * 2, per = 2 * (pw + ph), step = 19;
  const nP = Math.floor(per / step), crawl = t * 9;
  perim.amt = Math.max(0, perim.amt - dt / 2.8);
  for (let i = 0; i < nP; i++) {
    let s = (i * step + crawl) % per, x, y;
    if (s < pw) { x = inset + s; y = inset; } else if ((s -= pw) < ph) { x = W - inset; y = inset + s; }
    else if ((s -= ph) < pw) { x = W - inset - s; y = H - inset; if (Math.abs(x - L.cx) < L.chNowHW + 16) continue; } else { s -= pw; x = inset; y = H - inset - s; }
    if (y < 40 && (x < W * 0.62 || x > W - 230)) continue; // the system strip and the skin switch
    const pk = Math.pow(0.5 + 0.5 * Math.sin(i * 0.071 - t * 0.9), 10) + Math.pow(0.5 + 0.5 * Math.sin(i * 0.043 + t * 0.55 + 2), 14);
    heatCol(0.34 + 0.5 * pk, tmp);
    let r = tmp[0], g = tmp[1], b = tmp[2], a = 0.62 + 0.38 * pk;
    if (perim.amt > 0) { const pv = perim.amt * (0.45 + 0.55 * Math.sin(i * 0.5 - (t - perim.t0) * 10)); r = lerp(r, perim.r, pv); g = lerp(g, perim.g, pv); b = lerp(b, perim.b, pv); a = Math.max(a, pv); }
    const mt = Math.floor(t * (0.6 + 2.4 * h1(i)) + h1(i + 9) * 7);
    glyph(x, y, 13, 22.3, alienAt(i, mt), r, g, b, a * under(x, y));
  }

  // seams
  for (let arm = 0; arm < 4; arm++) {
    const dx = (arm === 0 || arm === 3 ? -1 : 1) * W / 2, dy = (arm < 2 ? -1 : 1) * H / 2;
    const len = Math.hypot(dx, dy), ux = dx / len, uy = dy / len;
    const startD = L.coreX * L.coreY / Math.hypot(L.coreY * Math.abs(ux), L.coreX * Math.abs(uy)) + 22;
    const endD = len - 40, sp = 25, cnt = Math.floor((endD - startD) / sp);
    const dir = arm % 2 ? -1 : 1, off = ((t * 16 * dir) % sp + sp) % sp;
    const base = Math.floor(t * 16 / sp) * dir;
    for (let j = 0; j < cnt; j++) {
      const d = startD + j * sp + off, id = j - base;
      const pk = Math.pow(0.5 + 0.5 * Math.sin(id * 0.55 + arm * 2.1), 6);
      heatCol(0.38 + 0.5 * pk, tmp);
      const edge = clamp((d - startD) / 30, 0, 1) * clamp((endD + sp - d) / 30, 0, 1);
      const gx2 = L.cx + ux * d, gy2 = L.cy + uy * d;
      glyph(gx2, gy2, 15, 25.7, alienAt(id * 3 + arm * 101, Math.floor(t * (0.4 + 1.5 * h1(id + arm * 31)))), tmp[0], tmp[1], tmp[2], (0.65 + 0.35 * pk) * edge * under(gx2, gy2));
    }
  }

  // sector sigils, names, trophies
  sectors.forEach((s, i) => {
    const [sx, sy] = L.sig[i];
    let r = 0, g = 0, b = 0;
    for (let k2 = 0; k2 < 4; k2++) { r += s.w[k2] * SEC_COL[k2][0]; g += s.w[k2] * SEC_COL[k2][1]; b += s.w[k2] * SEC_COL[k2][2]; }
    const blink = (1 - s.w[1] * 0.5 * (Math.floor(t * 3) % 2)) * under(sx, sy) * (s.p ? 1 : 0.35);
    const fl = s.flare, sc = 1 + fl * 1.1;
    glyph(sx, sy, 46 * sc, 79 * sc, s.sigil, lerp(r, 1, fl), lerp(g, 1, fl), lerp(b, 0.9, fl), blink);
    if (!s.p) return;
    const vertical = i === 1 || i === 3 || i === 2;
    const name = s.letter + (s.p.paused ? ' ·' : ''), f = 19, cw = f * 0.7;
    const lx = vertical ? sx : sx - 46 - (name.length - 1) * cw / 2, ly = vertical ? sy - 58 : sy;
    plate(lx, ly, name.length * cw + 10, 28, 0.92 * under(lx, ly));
    for (let j = 0; j < name.length; j++) if (name[j] !== ' ') glyph(lx - (name.length - 1) * cw / 2 + j * cw, ly, cw, 23, latinIdx(name[j]), HUD[0], HUD[1], HUD[2], under(lx, ly));
    for (let k2 = 0; k2 < s.tally; k2++) {
      const st2 = k2 === s.tally - 1 ? clamp(1 - (t - s.stampT) / 0.45, 0, 1) * (t > s.stampT ? 1 : 0) : 0, z = 1 + st2 * 2.2;
      const tx = vertical ? sx - (s.tally - 1) * 7 + k2 * 14 : sx + 46 + k2 * 14, ty = vertical ? sy + 58 : sy;
      plate(tx, ty, 15 * z, 24 * z, 0.9 * under(tx, ty));
      glyph(tx, ty, 11 * z, 18.9 * z, 44, lerp(HUD[0], 1, st2), lerp(HUD[1], 1, st2), lerp(HUD[2], 1, st2), 0.95 * under(tx, ty));
    }
  });

  // spawn tags: the agent's real name
  agents.forEach((a, i) => {
    if (a.hold) return;
    const sc = secTextCol(a.s), al = a.life * under(a.x, a.y);
    const text = a.name, f = 12, cw = f * 0.7, ch = f * 1.2;
    const R = L.agentR + 9;
    let x0, y;
    if (a.s === 0) { y = a.y - R - 10; x0 = a.x - (text.length - 1) * cw / 2; }
    else if (a.s === 2) { y = a.y + R + 10; x0 = a.x - (text.length - 1) * cw / 2; }
    else if (a.s === 1) { y = a.y; x0 = a.x + R + 10; }
    else { y = a.y; x0 = a.x - R - 10 - (text.length - 1) * cw; }
    x0 = clamp(x0, 30, W - 30 - text.length * cw);
    const age = now - a.tagT0, t2 = Math.floor(t * 14);
    heatCol(0.3 + 0.55 * a.heat, tmp);
    const stCol = a.state === 'fail' ? [1, 0.3, 0.2] : a.state === 'wait' ? [1, 0.75, 0.25] : a.state === 'hung' ? [0.55, 0.7, 1] : HUD;
    plate(x0 + (text.length - 1) * cw / 2, y, text.length * cw + 8, ch + 3, 0.92 * al);
    for (let j = 0; j < text.length; j++) {
      const c = text[j]; if (c === ' ') continue;
      const lat = age > 0.1 + j * 0.03;
      if (age < 0) continue;
      const col = j < 2 ? sc : stCol;
      if (lat) glyph(x0 + j * cw, y, cw, ch, latinIdx(c), col[0], col[1], col[2], 0.92 * al);
      else glyph(x0 + j * cw, y, cw * 0.86, ch * 0.86, alienAt(i * 50 + j, t2), sc[0], sc[1], sc[2], 0.8 * al);
    }
    if (a.state === 'work' && a.dieT < 0) {
      const cx2 = a.s === 3 ? x0 - cw * 2.2 : x0 + text.length * cw + 2;
      for (let j = 0; j < 2; j++) glyph(cx2 + j * 9, y, 8, 13.7, alienAt(i * 50 + 20 + j, Math.floor(t * (3 + j))), tmp[0], tmp[1], tmp[2], 0.7 * al);
    }
    // minutes of quiet, from the watcher (the panel's «N min», red while it says it works)
    const q = a.d?.quiet;
    if (q != null && q >= 3 && a.dieT < 0) {
      const s2 = `${q} MIN`, qx = a.s === 3 ? x0 - 8 - s2.length * cw : x0 + text.length * cw + (a.state === 'work' ? 24 : 6);
      for (let j = 0; j < s2.length; j++) if (s2[j] !== ' ') glyph(qx + j * cw, y, cw, ch, latinIdx(s2[j]), a.d.stuck ? 1 : 0.55, a.d.stuck ? 0.3 : 0.6, a.d.stuck ? 0.2 : 0.7, 0.9 * al);
    }
  });

  // marks: glyph inside each triangle; a group shows how many items stand behind it
  for (const g of vis) {
    const m = g.top, p = g._p, K = KIND[m.kind], b = m.burnT >= 0 ? clamp((now - m.burnT) / 0.9, 0, 1) : 0;
    const pul = 0.72 + 0.28 * Math.sin(t * 2.6 + m.id);
    glyph(p[0], p[1] - 2, 10, 17, K.glyph, lerp(K.col[0], 1, b), lerp(K.col[1], 1, b), lerp(K.col[2], 1, b), pul * (1 - b) * clamp((now - m.t0) / 0.2, 0, 1));
    if (g.n > 1 && !b) { const s2 = `x${g.n}`; plate(p[0] + 26 + s2.length * 4, p[1] - 22, s2.length * 8.4 + 6, 16, 0.85); for (let j = 0; j < s2.length; j++) glyph(p[0] + 26 + j * 8.4, p[1] - 22, 8.4, 14.4, latinIdx(s2[j]), K.col[0], K.col[1], K.col[2], 0.95); }
  }

  drawCoord(under);
  const centreHot = drawCentre();
  const tickHot = drawTicker();
  bandA = ease(bandA, tickHot ? 1 : 0, dt, 7);
  U.uBandA.value = bandA;

  drawActions();

  // channel handle glyph and content
  const ca = clamp((chan.o - 0.82) / 0.18, 0, 1);
  if (chan.o < 0.3) {
    const ha = 1 - chan.o / 0.3;
    glyph(L.cx - 12, H - 22, 10, 17, 11, 0.62, 0.5, 1, 0.9 * ha);
    glyph(L.cx + 2, H - 22, 7, 12, latinIdx('`'), 0.5, 0.42, 0.8, 0.8 * ha);
    glyph(L.cx + 12, H - 22, 7, 12, latinIdx('/'), 0.5, 0.42, 0.8, 0.8 * ha);
  }
  drawFeed(dt, ca);
  if (ca > 0.01) drawInput(ca);
  drawPackets();

  glyphGeo.instanceCount = gN;
  glyphGeo.attributes.aRect.needsUpdate = true;
  glyphGeo.attributes.aCol.needsUpdate = true;
  glyphGeo.attributes.aIdx.needsUpdate = true;
  if (centreHot) boost(0.05);
}

function drawActions() {
  labRects.length = 0;
  if (!lk || (shot && shot.st < 3) || ret.lock < 0.7) return;
  const acts = actionsFor(lk); if (!acts) return;
  const a = clamp((ret.lock - 0.7) / 0.3, 0, 1), age = now - lk.t0;
  const f = 14, cw = f * 0.7, ch = f * 1.2, R = lk.kind === 'core' ? ret.spread + 26 : ret.spread * 1.9 + 8 + 12;
  for (let i = 0; i < 3; i++) {
    const act = acts[i]; if (!act) continue;
    const ang = ROT0 + i * TAU / 3, isArmed = armed && armed.k === i + 1;
    const label = cut(act.label, 22) + (act.stage2 ? ' · STAGE 2' : '');
    const text = isArmed ? `${i + 1} ${label}? ${i + 1} AGAIN` : `${i + 1} ${label}`;
    const n = text.length, w = n * cw;
    const ax = ret.x + Math.cos(ang) * R, ay = ret.y + Math.sin(ang) * R;
    let x0 = i === 0 ? ax - w : i === 1 ? ax : ax - w / 2;
    let y = i === 2 ? ay + 6 : ay - 4;
    x0 = clamp(x0, 26, W - 26 - w);
    const inCore = (px, py) => Math.abs(px - L.cx) / (L.coreX + 14) + Math.abs(py - L.cy) / (L.coreY + 14) < 1;
    for (let g = 0; g < 40 && [x0, x0 + w].some((px) => inCore(px, y - ch / 2) || inCore(px, y + ch / 2)); g++) y += (y < L.cy ? -5 : 5);
    glyph(x0 + w / 2 - cw / 2, y, w + 14, ch + 6, SOLID, 0, 0, 0, 0.8 * a);
    const firing = fx && fx.k === i + 1;
    for (let j = 0; j < n; j++) {
      const c = text[j]; if (c === ' ') continue;
      const lat = age > 0.45 + (i * 6 + j) * 0.012;
      let col = j === 0 ? [1, 0.78, 0.1] : act.danger ? [1, 0.55, 0.45] : [1, 0.97, 0.88];
      let al = a;
      if (act.stage2 || act.label === '—') { col = [0.45, 0.5, 0.55]; al *= 0.7; }
      if (isArmed) { col = j === 0 ? [1, 0.9, 0.3] : [1, 0.2, 0.12]; al *= Math.floor(now * 6) % 2 ? 0.6 : 1; }
      if (firing) col = [1, 1, 0.95];
      if (fx && !firing) al *= 0.35;
      if (lat) glyph(x0 + j * cw, y, cw, ch, latinIdx(c), col[0], col[1], col[2], al);
      else glyph(x0 + j * cw, y, cw * 0.86, ch * 0.86, alienAt(i * 9 + j, Math.floor(now * 18)), 1, 0.4, 0.1, al * 0.9);
    }
    labRects.push({ k: i + 1, x0: x0 - 10, x1: x0 + w + 4, y0: y - ch / 2 - 6, y1: y + ch / 2 + 6 });
  }
  if (age < 0.9) boost(0.05);
}

/* ---------- loop: slow at rest, fast in a transition, stopped when hidden or in the other skin ---------- */
let rafId = 0, timerId = 0, lastT = performance.now() / 1000, running = false, lastFull = 0, dirty = false;
function frame() {
  rafId = 0; timerId = 0;
  if (document.hidden || !shown) { running = false; return; }
  const c0 = performance.now(), tNow = c0 / 1000;
  const dtReal = tNow - lastT; lastT = tNow;
  const dt = Math.min(dtReal, 0.12);
  now += dt;
  update(dt);
  const full = now < hotUntil || tNow - lastFull >= 1 / REST_FPS - 0.012;
  if (full) {
    const dF = tNow - lastFull; lastFull = tNow;
    if (dF > 0 && dF < 1) perf.fullFps = perf.fullFps ? lerp(perf.fullFps, 1 / dF, 0.25) : 1 / dF;
    renderer.render(scene, camera);
  } else {
    const r = L.scis;
    renderer.setScissorTest(true); renderer.setScissor(r.x, r.y, r.w, r.h);
    renderer.render(scene, camera);
    renderer.setScissorTest(false);
  }
  const ms = performance.now() - c0;
  perf.ms = perf.ms ? lerp(perf.ms, ms, 0.2) : ms;
  if (dtReal > 0) perf.fps = perf.fps ? lerp(perf.fps, 1 / dtReal, 0.25) : 1 / dtReal;
  perf.calls = renderer.info.render.calls;
  if (now - lastSlow > 0.5) { lastSlow = now; slowDom(); }
  schedule();
}
function schedule() {
  running = true;
  if (rafId || timerId) return;
  if (now < hotUntil || now < inputHotUntil) rafId = requestAnimationFrame(frame);
  else timerId = setTimeout(() => { timerId = 0; rafId = requestAnimationFrame(frame); }, 1000 / REST_FPS - 8);
}
function wake() {
  if (!shown) return;
  if (timerId) { clearTimeout(timerId); timerId = 0; }
  if (!rafId && !document.hidden) rafId = requestAnimationFrame(frame);
}
document.addEventListener('visibilitychange', () => {
  if (document.hidden) { if (rafId) cancelAnimationFrame(rafId); if (timerId) clearTimeout(timerId); rafId = timerId = 0; running = false; }
  else if (shown) { lastT = performance.now() / 1000; if (!running) schedule(); }
});
window.addEventListener('resize', () => { if (shown) { resize(); wake(); } });

export function attach(st, a) {
  store = st; api = a;
  store.on(() => { dirty = true; wake(); });
  dirty = true;
  resize();
  ret.x = W * 0.7; ret.y = H * 0.3;
  updateChan(0);
  hints();
  return {
    show() { shown = true; resize(); lastT = performance.now() / 1000; boost(1); schedule(); wake(); },
    hide() { shown = false; if (rafId) cancelAnimationFrame(rafId); if (timerId) clearTimeout(timerId); rafId = timerId = 0; running = false; if (document.activeElement === cmdEl) cmdEl.blur(); convEl.hidden = teaserEl.hidden = dotEl.hidden = true; conv.visible = false; },
  };
}

// for checks from the dev tools (read-only views; actions go through the reticle)
window.hunterConsole = {
  info: () => ({ sectors: sectors.map((s) => `${s.letter}:${s.look}`), marks: openMarks().length, groups: groups().map((g) => `${SL(g.top.s)}:${g.top.kind}×${g.n}`),
    agents: liveAgents().map((a) => `${a.name}:${a.state}`), lock: lk && lk.kind, chan: chan.oT, glyphs: gN, hot: now < hotUntil, calls: renderer.info.render.calls, fps: perf.fps, fullFps: perf.fullFps }),
  perf, cycle, openChan, closeChan, recall, lockSector: (s) => setLock({ kind: 'sector', s }), lockCore: () => setLock({ kind: 'core' }),
  lockAgent: (i) => setLock({ kind: 'agent', a: liveAgents()[i] }), lockMark: (i) => setLock({ kind: 'mark', m: openMarks()[i] }), fire, fireLater,
};
