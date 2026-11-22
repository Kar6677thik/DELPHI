import { Html } from '@react-three/drei';
import { Canvas, useFrame } from '@react-three/fiber';
import { Bloom, EffectComposer, Vignette } from '@react-three/postprocessing';
import { useEffect, useMemo, useRef } from 'react';
import * as THREE from 'three';

import { forDark } from '../components/HypothesisBar';
import type { ReplayHypothesis } from './replay';

/**
 * The Oracle: DELPHI's belief, drawn as light (the Theatre, act 1).
 *
 * Each hypothesis is a well; Unexplained sits at the centre. N particles are the probability
 * mass, each one 1/N of it, so a well holds as many particles as its posterior share. When the
 * posterior changes, only the surplus moves: particles leave the wells that lost mass and fly,
 * on curved paths, to the wells that gained it. A Bayes update you can watch. At the decision,
 * the winning well sends out a shockwave.
 */

const N = 4000;
// The ring's half-width and half-height: it sits between the phone and the side panel.
const RING_X = 3.7;
const RING_Y = 3.05;
const FLIGHT_S = 1.9;
const STAGGER_S = 0.9;
const UNEXPLAINED = 'UNEXPLAINED';

interface Well {
  key: string;
  name: string;
  center: THREE.Vector3;
  color: THREE.Color;
}

function wellsFor(hypotheses: readonly ReplayHypothesis[]): Well[] {
  const ring = hypotheses.filter((h) => h.key !== UNEXPLAINED);
  // GENUINE on the far left, so belief visibly swings across the stage.
  const start = Math.PI;
  return hypotheses.map((h) => {
    const i = ring.findIndex((r) => r.key === h.key);
    const center =
      i < 0
        ? new THREE.Vector3(0, 0, 0)
        : new THREE.Vector3(
            Math.cos(start - (i * 2 * Math.PI) / ring.length) * RING_X,
            Math.sin(start - (i * 2 * Math.PI) / ring.length) * RING_Y + 0.35,
            0,
          );
    return { key: h.key, name: h.name, center, color: new THREE.Color(forDark(h.color)) };
  });
}

/** Largest-remainder counts: exactly N particles, each well as close to its share as it gets. */
function counts(shares: number[]): number[] {
  const total = shares.reduce((a, b) => a + b, 0) || 1;
  const raw = shares.map((s) => (s / total) * N);
  const out = raw.map(Math.floor);
  let left = N - out.reduce((a, b) => a + b, 0);
  const order = raw.map((r, i) => [r - Math.floor(r), i] as const).sort((a, b) => b[0] - a[0]);
  for (const [, i] of order) {
    if (left <= 0) break;
    out[i] = (out[i] ?? 0) + 1;
    left -= 1;
  }
  return out;
}

interface Particle {
  well: number;
  from: THREE.Vector3;
  fromColor: THREE.Color;
  start: number; // flight start (s); < 0: at rest in its well
  radius: number;
  phase: number;
  speed: number;
  tilt: number;
  bob: number;
}

const VERTEX = /* glsl */ `
  attribute vec3 color;
  attribute float size;
  varying vec3 vColor;
  void main() {
    vColor = color;
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    gl_PointSize = size * (220.0 / -mv.z);
    gl_Position = projectionMatrix * mv;
  }
`;
const FRAGMENT = /* glsl */ `
  varying vec3 vColor;
  void main() {
    float d = length(gl_PointCoord - 0.5);
    float a = smoothstep(0.5, 0.0, d);
    gl_FragColor = vec4(vColor * (0.45 + 0.6 * a), a * a * 0.55);
  }
`;

function easeInOut(t: number): number {
  return t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2;
}

/**
 * The particles and their motion, as a plain object: animation state that changes every frame,
 * so it lives outside React's state (held in a ref, built in an effect).
 */
class Cloud {
  readonly wells: Well[];
  readonly particles: Particle[] = [];
  clock = 0;

  constructor(wells: Well[], shares: number[]) {
    this.wells = wells;
    counts(shares).forEach((n, w) => {
      for (let i = 0; i < n; i += 1) {
        this.particles.push({
          well: w,
          from: wells[w]?.center.clone() ?? new THREE.Vector3(),
          fromColor: wells[w]?.color.clone() ?? new THREE.Color(),
          start: -1,
          radius: Math.abs(gaussian()) * 0.62 + 0.08,
          phase: Math.random() * Math.PI * 2,
          speed: 0.25 + Math.random() * 0.6,
          tilt: (Math.random() - 0.5) * 1.6,
          bob: Math.random() * Math.PI * 2,
        });
      }
    });
  }

  /** A new posterior: move just the surplus, from the wells that lost mass to those that gained. */
  retarget(shares: number[]): void {
    const { particles, wells } = this;
    const target = counts(shares);
    const have = wells.map((_, w) => particles.filter((p) => p.well === w).length);
    const donors: number[] = [];
    have.forEach((n, w) => {
      const surplus = n - (target[w] ?? 0);
      if (surplus > 0) {
        donors.push(
          ...particles
            .map((p, i) => [p, i] as const)
            .filter(([p]) => p.well === w)
            .sort(() => Math.random() - 0.5)
            .slice(0, surplus)
            .map(([, i]) => i),
        );
      }
    });
    donors.sort(() => Math.random() - 0.5);
    let d = 0;
    target.forEach((n, w) => {
      const need = n - (have[w] ?? 0);
      for (let k = 0; k < need && d < donors.length; k += 1) {
        const p = particles[donors[d] ?? 0];
        d += 1;
        if (!p) continue;
        p.from = orbit(p, wells, this.clock, 1);
        p.fromColor = wells[p.well]?.color.clone() ?? new THREE.Color();
        p.well = w;
        p.start = this.clock + Math.random() * STAGGER_S;
      }
    });
  }

  /** Advance the clock and write every particle's position and colour into the geometry. */
  step(delta: number, geometry: THREE.BufferGeometry, decided: boolean): void {
    this.clock += Math.min(delta, 0.05);
    const t = this.clock;
    const { particles, wells } = this;
    const pos = geometry.getAttribute('position') as THREE.BufferAttribute;
    const col = geometry.getAttribute('color') as THREE.BufferAttribute;
    const tight = decided ? 0.55 : 1;
    for (let i = 0; i < particles.length; i += 1) {
      const p = particles[i];
      if (!p) continue;
      const home = orbit(p, wells, t, tight);
      const well = wells[p.well];
      let x = home.x;
      let y = home.y;
      let z = home.z;
      let c = well?.color ?? WHITE;
      if (p.start >= 0) {
        const k = (t - p.start) / FLIGHT_S;
        if (k < 0) {
          x = p.from.x;
          y = p.from.y;
          z = p.from.z;
          c = p.fromColor;
        } else if (k < 1) {
          const e = easeInOut(k);
          // A curved path, lifted towards the viewer so streams don't cut through the wells.
          const mx = (p.from.x + home.x) / 2;
          const my = (p.from.y + home.y) / 2 + 0.9;
          const mz = 2.4;
          const a = 1 - e;
          x = a * a * p.from.x + 2 * a * e * mx + e * e * home.x;
          y = a * a * p.from.y + 2 * a * e * my + e * e * home.y;
          z = a * a * p.from.z + 2 * a * e * mz + e * e * home.z;
          c = MIX.copy(p.fromColor).lerp(well?.color ?? WHITE, e);
        } else {
          p.start = -1;
        }
      }
      pos.setXYZ(i, x, y, z);
      col.setXYZ(i, c.r, c.g, c.b);
    }
    pos.needsUpdate = true;
    col.needsUpdate = true;
  }
}

/** A fixed, varied size per particle (a hash of its index, so rendering stays pure). */
function sizeOf(i: number): number {
  const h = Math.sin(i * 12.9898) * 43758.5453;
  return 0.6 + (h - Math.floor(h)) * 1.1;
}

function Belief({ wells, shares, decided }: { wells: Well[]; shares: number[]; decided: boolean }) {
  const cloud = useRef<Cloud | null>(null);
  const geometry = useMemo(() => {
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(new Float32Array(N * 3), 3));
    g.setAttribute('color', new THREE.BufferAttribute(new Float32Array(N * 3), 3));
    g.setAttribute(
      'size',
      new THREE.BufferAttribute(
        Float32Array.from({ length: N }, (_, i) => sizeOf(i)),
        1,
      ),
    );
    return g;
  }, []);
  const material = useMemo(
    () =>
      new THREE.ShaderMaterial({
        vertexShader: VERTEX,
        fragmentShader: FRAGMENT,
        transparent: true,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
      }),
    [],
  );

  useEffect(() => {
    if (cloud.current?.wells !== wells) cloud.current = new Cloud(wells, shares);
    else cloud.current.retarget(shares);
  }, [wells, shares]);

  useFrame((_, delta) => {
    cloud.current?.step(delta, geometry, decided);
  });

  return <points geometry={geometry} material={material} />;
}

const WHITE = new THREE.Color('#ffffff');
const MIX = new THREE.Color();
const SCRATCH = new THREE.Vector3();

function gaussian(): number {
  const u = 1 - Math.random();
  const v = Math.random();
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
}

/** Where a resting particle is: orbiting its well, on its own tilted ring. */
function orbit(p: Particle, wells: Well[], t: number, tight: number): THREE.Vector3 {
  const c = wells[p.well]?.center ?? SCRATCH.set(0, 0, 0);
  const a = p.phase + t * p.speed;
  const r = p.radius * tight * (1 + 0.08 * Math.sin(t * 1.3 + p.bob));
  return new THREE.Vector3(
    c.x + Math.cos(a) * r,
    c.y + Math.sin(a) * r * Math.cos(p.tilt),
    c.z + Math.sin(a) * r * Math.sin(p.tilt),
  );
}

/** The well's name and share, floating under it; its faint ring grows with its mass. */
function WellMark({ well, share, lead }: { well: Well; share: number; lead: boolean }) {
  const ring = useRef<THREE.Mesh>(null);
  useFrame(() => {
    if (!ring.current) return;
    const target = 0.35 + Math.sqrt(share) * 1.25;
    const s = ring.current.scale.x + (target - ring.current.scale.x) * 0.06;
    ring.current.scale.set(s, s, s);
  });
  const hex = `#${well.color.getHexString()}`;
  // Under the ring, which grows with the share, so the label never sits on the bright core.
  const below = -(0.35 + Math.sqrt(share) * 1.25) - (lead ? 0.75 : 0.45);
  return (
    <group position={well.center}>
      <mesh ref={ring}>
        <ringGeometry args={[0.98, 1, 96]} />
        <meshBasicMaterial color={well.color} transparent opacity={0.22} />
      </mesh>
      <Html center position={[0, below, 0]} zIndexRange={[10, 0]} style={{ pointerEvents: 'none' }}>
        <div
          className="whitespace-nowrap text-center transition-opacity duration-700"
          style={{
            opacity: 0.35 + Math.min(1, share * 3) * 0.65,
            textShadow: '0 0 12px #050507, 0 0 4px #050507',
          }}
        >
          <div
            className={`figure leading-none ${lead ? 'text-[34px]' : 'text-[18px]'}`}
            style={{ color: hex }}
          >
            {(share * 100).toFixed(share >= 0.1 ? 0 : 1)}%
          </div>
          <div className={`mt-1 text-white/70 ${lead ? 'text-[14px]' : 'text-[12px]'}`}>
            {well.name}
          </div>
        </div>
      </Html>
    </group>
  );
}

/** The decision: a ring of light expanding out of the winning well. */
function Shockwave({ well, at }: { well: Well; at: number }) {
  const mesh = useRef<THREE.Mesh>(null);
  const material = useRef<THREE.MeshBasicMaterial>(null);
  const born = useRef<number | null>(null);
  useFrame((state) => {
    if (born.current === null) born.current = state.clock.elapsedTime;
    const k = (state.clock.elapsedTime - born.current) / 2.2;
    if (!mesh.current || !material.current) return;
    const s = 0.5 + k * 9;
    mesh.current.scale.set(s, s, s);
    material.current.opacity = Math.max(0, 0.7 * (1 - k));
  });
  return (
    <mesh ref={mesh} position={well.center} key={at}>
      <ringGeometry args={[0.96, 1, 128]} />
      <meshBasicMaterial ref={material} color={well.color} transparent />
    </mesh>
  );
}

function Drift() {
  useFrame((state) => {
    const t = state.clock.elapsedTime;
    state.camera.position.x = Math.sin(t * 0.07) * 1.2;
    state.camera.position.y = Math.cos(t * 0.05) * 0.6 - 0.4;
    state.camera.lookAt(0, -0.3, 0);
  });
  return null;
}

export function Oracle({
  hypotheses,
  posterior,
  decision,
}: {
  hypotheses: readonly ReplayHypothesis[];
  posterior: Record<string, number>;
  /** Set once the session is decided: the winning well's key. */
  decision: string | null;
}) {
  const wells = useMemo(() => wellsFor(hypotheses), [hypotheses]);
  const total = Object.values(posterior).reduce((a, b) => a + b, 0) || 1;
  const sharesKey = wells.map((w) => (posterior[w.key] ?? 0).toFixed(4)).join(',');
  const shares = useMemo(
    () => sharesKey.split(',').map(Number),
    // Only a real change of the numbers moves particles.
    [sharesKey],
  );
  const leadIndex = shares.indexOf(Math.max(...shares));
  const winner = decision ? wells[leadIndex] : undefined;
  return (
    <Canvas
      camera={{ position: [0, -0.4, 12.5], fov: 50 }}
      dpr={[1, 2]}
      gl={{ antialias: false, powerPreference: 'high-performance' }}
      aria-label="DELPHI's belief: particles grouped by hypothesis"
    >
      <color attach="background" args={['#050507']} />
      <fog attach="fog" args={['#050507', 14, 26]} />
      <Drift />
      <Belief wells={wells} shares={shares} decided={decision !== null} />
      {wells.map((w, i) => (
        <WellMark key={w.key} well={w} share={(shares[i] ?? 0) / total} lead={i === leadIndex} />
      ))}
      {winner && <Shockwave key={decision} well={winner} at={0} />}
      <EffectComposer multisampling={0}>
        <Bloom mipmapBlur intensity={0.75} luminanceThreshold={0.22} luminanceSmoothing={0.35} />
        <Vignette offset={0.25} darkness={0.85} />
      </EffectComposer>
    </Canvas>
  );
}
