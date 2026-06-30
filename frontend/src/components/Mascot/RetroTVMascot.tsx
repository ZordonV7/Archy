import { useEffect, useRef } from "react";

/**
 * RetroTVMascot — a retro CRT TV character with 7 canvas-animated moods.
 *
 * Ported from retro_tv_mascot_v4.html. The TV body (shell, screen, eyes,
 * mouth, FX canvas) is self-contained; the demo wrapper, control buttons,
 * and status bar from the original HTML are stripped out.
 *
 * The `mood` prop drives everything: eye shape, mouth shape, screen glow
 * color, background stripe pattern, body animation (float/droop/shake/tilt),
 * and particle FX (stars/tears/static/bubbles/zzz).
 *
 * Mood transitions are cross-faded over 0.55s for smooth visual handoff.
 */

export type RetroMood =
  | "happy"
  | "sad"
  | "panic"
  | "upset"
  | "thinking"
  | "sleeping"
  | "waking";

// --- Mood definitions (colors, bg, stripes, body anim, status) ---

const moodDefs: Record<
  RetroMood,
  {
    color: string;
    bg: string;
    stripes: string;
    bodyAnim: "float" | "droop" | "shake" | "tilt" | "tired";
    status: string;
  }
> = {
  happy: {
    color: "#00ffb4",
    bg: "rgba(0,255,180,.05)",
    stripes:
      "repeating-linear-gradient(0deg,rgba(0,255,100,.07) 0,rgba(0,255,100,.07) 7px,rgba(0,180,80,.03) 7px,rgba(0,180,80,.03) 14px,transparent 14px,transparent 22px)",
    bodyAnim: "float",
    status: "// MOOD: ELATED // SYSTEMS NOMINAL //",
  },
  sad: {
    color: "#4dc8ff",
    bg: "rgba(50,140,255,.04)",
    stripes:
      "repeating-linear-gradient(0deg,rgba(40,100,200,.07) 0,rgba(40,100,200,.07) 6px,transparent 6px,transparent 14px)",
    bodyAnim: "droop",
    status: "// MOOD: MELANCHOLIC // TEARDUCT: ACTIVE //",
  },
  panic: {
    color: "#ff4466",
    bg: "rgba(255,30,70,.07)",
    stripes:
      "repeating-linear-gradient(0deg,rgba(255,20,50,.1) 0,rgba(255,20,50,.1) 4px,transparent 4px,transparent 8px)",
    bodyAnim: "shake",
    status: "// ALERT: CRITICAL FAILURE // DESTABILIZED //",
  },
  upset: {
    color: "#ff8800",
    bg: "rgba(255,110,0,.05)",
    stripes:
      "repeating-linear-gradient(0deg,rgba(255,90,0,.08) 0,rgba(255,90,0,.08) 5px,transparent 5px,transparent 11px)",
    bodyAnim: "tilt",
    status: "// MOOD: IRRITATED // PATIENCE.EXE: NULL //",
  },
  thinking: {
    color: "#c880ff",
    bg: "rgba(170,70,255,.04)",
    stripes:
      "repeating-linear-gradient(0deg,rgba(130,50,220,.07) 0,rgba(130,50,220,.07) 6px,transparent 6px,transparent 16px)",
    bodyAnim: "tilt",
    status: "// PROCESSING... // NEURAL LOAD: 99.9% //",
  },
  sleeping: {
    color: "#6688ff",
    bg: "rgba(50,70,220,.04)",
    stripes:
      "repeating-linear-gradient(0deg,rgba(40,55,180,.07) 0,rgba(40,55,180,.07) 8px,transparent 8px,transparent 24px)",
    bodyAnim: "droop",
    status: "// STANDBY MODE // ZZZ.EXE RUNNING //",
  },
  waking: {
    color: "#ffdd44",
    bg: "rgba(255,210,30,.04)",
    stripes:
      "repeating-linear-gradient(0deg,rgba(255,190,20,.06) 0,rgba(255,190,20,.06) 6px,transparent 6px,transparent 14px)",
    bodyAnim: "tired",
    status: "// BOOT SEQUENCE // STILL LOADING... //",
  },
};

// --- Body animation keyframes ---

const bodyKeyframes: Record<
  "float" | "droop" | "shake" | "tilt" | "tired",
  (p: number) => { y: number; r: number; x?: number }
> = {
  float: (p) => ({ y: Math.sin(p * Math.PI * 2) * -7, r: 0 }),
  droop: (p) => ({
    y: Math.sin(p * Math.PI * 2 + 1) * 3,
    r: Math.sin(p * Math.PI * 2) * 1.5 * (Math.PI / 180),
  }),
  shake: (p) => ({
    y: 0,
    r: Math.sin(p * Math.PI * 2 * 4) * 1.5 * (Math.PI / 180),
    x: Math.sin(p * Math.PI * 2 * 4.2) * 5,
  }),
  tilt: (p) => ({
    y: Math.sin(p * Math.PI * 2) * -2,
    r: Math.sin(p * Math.PI * 2) * 3 * (Math.PI / 180),
  }),
  tired: (p) => ({
    y: Math.sin(p * Math.PI * 2) * 2.5,
    r: Math.sin(p * Math.PI * 2 * 0.5) * 1.2 * (Math.PI / 180),
    x: Math.sin(p * Math.PI * 2 * 0.3) * 1.5,
  }),
};

const bodyPeriods: Record<string, number> = {
  float: 3,
  droop: 4,
  shake: 0.45,
  tilt: 2.8,
  tired: 5,
};

// --- Drawing helpers ---

function hex2rgb(h: string): [number, number, number] {
  return [
    parseInt(h.slice(1, 3), 16),
    parseInt(h.slice(3, 5), 16),
    parseInt(h.slice(5, 7), 16),
  ];
}

function ease(t: number): number {
  return t < 0.5 ? 2 * t * t : -1 + (4 - 2 * t) * t;
}

function glowStroke(
  ctx: CanvasRenderingContext2D,
  color: string,
  lw: number,
  fn: () => void
) {
  ctx.save();
  ctx.shadowColor = color;
  ctx.shadowBlur = 9;
  ctx.strokeStyle = color;
  ctx.lineWidth = lw;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  fn();
  ctx.shadowBlur = 18;
  ctx.lineWidth = lw * 0.4;
  ctx.strokeStyle = color + "66";
  fn();
  ctx.restore();
}

function glowFill(
  ctx: CanvasRenderingContext2D,
  color: string,
  fn: () => void
) {
  ctx.save();
  ctx.shadowColor = color;
  ctx.shadowBlur = 10;
  ctx.fillStyle = color;
  fn();
  ctx.restore();
}

// --- Eye drawing per mood ---

function drawEyeForMood(
  ctx: CanvasRenderingContext2D,
  mood: RetroMood,
  side: "L" | "R",
  t: number,
  alpha: number
) {
  if (alpha <= 0) return;
  ctx.save();
  ctx.globalAlpha = alpha;
  const w = ctx.canvas.width,
    h = ctx.canvas.height,
    cx = w / 2,
    cy = h / 2;
  const c = moodDefs[mood].color;

  if (mood === "waking") {
    const blinkDip = Math.sin(t * 0.7) * 0.08;
    const openFrac = 0.58 + blinkDip;
    const eyeR = 10;
    const lidY = cy + eyeR - openFrac * eyeR * 2;

    glowStroke(ctx, c, 2, () => {
      ctx.beginPath();
      ctx.arc(cx, cy, eyeR, 0, Math.PI * 2);
      ctx.stroke();
    });

    ctx.save();
    ctx.beginPath();
    ctx.rect(0, lidY, w, h);
    ctx.clip();
    const px = cx + Math.sin(t * 0.4) * 1.8;
    const py = cy + Math.cos(t * 0.3) * 1;
    glowFill(ctx, c, () => {
      ctx.beginPath();
      ctx.arc(px, py, 3, 0, Math.PI * 2);
      ctx.fill();
    });
    ctx.save();
    ctx.globalAlpha = 0.35;
    glowStroke(ctx, c, 1, () => {
      ctx.beginPath();
      ctx.arc(cx, cy, 5.5, 0, Math.PI * 2);
      ctx.stroke();
    });
    ctx.restore();
    ctx.restore();

    ctx.fillStyle = "#04040e";
    ctx.beginPath();
    ctx.arc(cx, cy, eyeR + 2, Math.PI, 0, false);
    ctx.lineTo(cx + eyeR + 2, lidY);
    ctx.lineTo(cx - eyeR - 2, lidY);
    ctx.closePath();
    ctx.fill();

    const lidAng = Math.asin(
      Math.max(-1, Math.min(1, (cy - lidY) / eyeR))
    );
    glowStroke(ctx, c, 2.2, () => {
      ctx.beginPath();
      ctx.arc(cx, cy, eyeR, Math.PI - lidAng, lidAng, false);
      ctx.stroke();
    });

    glowStroke(ctx, c, 2.8, () => {
      ctx.beginPath();
      ctx.moveTo(cx - eyeR - 1, lidY);
      ctx.lineTo(cx + eyeR + 1, lidY);
      ctx.stroke();
    });
  } else if (mood === "happy") {
    const bob = Math.sin(t * 2.5) * 1.8;
    glowStroke(ctx, c, 2.5, () => {
      ctx.beginPath();
      ctx.arc(cx, cy + 4 + bob, 10, Math.PI, 0, false);
      ctx.stroke();
    });
  } else if (mood === "sad") {
    const droop = Math.sin(t * 1.2) * 1.5;
    glowStroke(ctx, c, 2.5, () => {
      ctx.beginPath();
      ctx.arc(cx, cy - 2 + droop, 9, 0, Math.PI, false);
      ctx.stroke();
    });
    const tear = Math.abs(Math.sin(t * 1.8 + (side === "L" ? 0 : 1.3))) * 3;
    if (tear > 1.5)
      glowFill(ctx, c, () => {
        ctx.beginPath();
        ctx.ellipse(
          cx + (side === "L" ? 2 : -2),
          cy + 11 + tear,
          1.5,
          tear * 0.8,
          0,
          0,
          Math.PI * 2
        );
        ctx.fill();
      });
  } else if (mood === "panic") {
    const jx = (Math.random() - 0.5) * 3,
      jy = (Math.random() - 0.5) * 3;
    ctx.save();
    ctx.translate(jx, jy);
    glowStroke(ctx, c, 2.8, () => {
      ctx.beginPath();
      ctx.moveTo(cx - 9, cy - 7);
      ctx.lineTo(cx + 9, cy + 7);
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(cx + 9, cy - 7);
      ctx.lineTo(cx - 9, cy + 7);
      ctx.stroke();
    });
    ctx.restore();
    ctx.save();
    ctx.globalAlpha = 0.4 + Math.abs(Math.sin(t * 8)) * 0.6;
    glowStroke(ctx, c, 1, () => {
      ctx.beginPath();
      ctx.arc(cx, cy, 13, 0, Math.PI * 2);
      ctx.stroke();
    });
    ctx.restore();
  } else if (mood === "upset") {
    const ang = side === "L" ? -0.35 : 0.35;
    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(ang);
    ctx.translate(-cx, -cy);
    glowStroke(ctx, c, 2.8, () => {
      ctx.beginPath();
      ctx.moveTo(cx - 10, cy - 6);
      ctx.lineTo(cx + 10, cy - 6);
      ctx.stroke();
    });
    const ry = Math.sin(t * 1.4) > 0.7 ? 1.5 : 7;
    glowStroke(ctx, c, 2.2, () => {
      ctx.beginPath();
      ctx.ellipse(cx, cy + 4, 8, ry, 0, 0, Math.PI * 2);
      ctx.stroke();
    });
    ctx.restore();
  } else if (mood === "thinking") {
    const lx = Math.sin(t * 0.6) * 5,
      ly = Math.cos(t * 0.4) * 2;
    glowStroke(ctx, c, 2.2, () => {
      ctx.beginPath();
      ctx.arc(cx, cy, 10, 0, Math.PI * 2);
      ctx.stroke();
    });
    glowFill(ctx, c, () => {
      ctx.beginPath();
      ctx.arc(cx + lx, cy + ly, 3.5, 0, Math.PI * 2);
      ctx.fill();
    });
  } else if (mood === "sleeping") {
    const br = Math.sin(t * 0.5) * 0.8;
    glowStroke(ctx, c, 2.5, () => {
      ctx.beginPath();
      ctx.arc(cx, cy + 2 - br, 9, Math.PI, 0, true);
      ctx.stroke();
    });
  }
  ctx.restore();
}

// --- Mouth drawing per mood ---

function drawMouthForMood(
  ctx: CanvasRenderingContext2D,
  mood: RetroMood,
  t: number,
  alpha: number
) {
  if (alpha <= 0) return;
  ctx.save();
  ctx.globalAlpha = alpha;
  const w = ctx.canvas.width,
    h = ctx.canvas.height,
    cx = w / 2,
    cy = h / 2;
  const c = moodDefs[mood].color;

  if (mood === "waking") {
    const yawnCycle = Math.sin(t * 0.5) * 0.5 + 0.5;
    const yawn = Math.pow(yawnCycle, 1.4);
    const jawDrop = yawn * 9;
    const mouthW = 16 + yawn * 4;

    glowStroke(ctx, c, 2.5, () => {
      ctx.beginPath();
      ctx.moveTo(cx - mouthW, cy - (jawDrop * 0.1));
      ctx.quadraticCurveTo(cx, cy + jawDrop, cx + mouthW, cy - (jawDrop * 0.1));
      ctx.stroke();
    });
    glowStroke(ctx, c, 1.8, () => {
      ctx.beginPath();
      ctx.moveTo(cx - mouthW, cy - (jawDrop * 0.1));
      ctx.quadraticCurveTo(cx, cy - 3 - yawn * 2, cx + mouthW, cy - (jawDrop * 0.1));
      ctx.stroke();
    });
    if (jawDrop > 3) {
      ctx.save();
      ctx.fillStyle = c;
      ctx.globalAlpha = yawn * 0.1;
      ctx.beginPath();
      ctx.moveTo(cx - mouthW, cy - (jawDrop * 0.1));
      ctx.quadraticCurveTo(cx, cy + jawDrop, cx + mouthW, cy - (jawDrop * 0.1));
      ctx.quadraticCurveTo(cx, cy - 3 - yawn * 2, cx - mouthW, cy - (jawDrop * 0.1));
      ctx.closePath();
      ctx.fill();
      ctx.restore();
    }
  } else if (mood === "happy") {
    const s = 1 + Math.sin(t * 2.5) * 0.07;
    glowStroke(ctx, c, 2.8, () => {
      ctx.beginPath();
      ctx.moveTo(cx - 22 * s, cy - 4);
      ctx.quadraticCurveTo(cx, cy + 14 * s, cx + 22 * s, cy - 4);
      ctx.stroke();
    });
    ctx.save();
    ctx.fillStyle = c;
    ctx.globalAlpha = 0.15 + Math.sin(t * 2.5) * 0.05;
    ctx.beginPath();
    ctx.moveTo(cx - 22 * s, cy - 4);
    ctx.quadraticCurveTo(cx, cy + 14 * s, cx + 22 * s, cy - 4);
    ctx.closePath();
    ctx.fill();
    ctx.restore();
  } else if (mood === "sad") {
    const q = Math.sin(t * 3) * 0.8;
    glowStroke(ctx, c, 2.8, () => {
      ctx.beginPath();
      ctx.moveTo(cx - 20, cy + 8);
      ctx.quadraticCurveTo(cx + q, cy - 6, cx + 20, cy + 8);
      ctx.stroke();
    });
  } else if (mood === "panic") {
    const sh = 8 + Math.abs(Math.sin(t * 7)) * 6;
    glowStroke(ctx, c, 2.5, () => {
      ctx.beginPath();
      ctx.ellipse(cx, cy, 16, sh, 0, 0, Math.PI * 2);
      ctx.stroke();
    });
    ctx.save();
    ctx.fillStyle = c;
    ctx.globalAlpha = 0.18 + Math.sin(t * 7) * 0.08;
    ctx.beginPath();
    ctx.ellipse(cx, cy, 16, sh, 0, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
  } else if (mood === "upset") {
    const pr = Math.sin(t * 1.4) * 0.5;
    glowStroke(ctx, c, 2.8, () => {
      ctx.beginPath();
      ctx.moveTo(cx - 22, cy + pr);
      ctx.lineTo(cx + 22, cy + pr);
      ctx.stroke();
    });
    glowStroke(ctx, c, 1.5, () => {
      ctx.beginPath();
      ctx.moveTo(cx - 10, cy - 3 + pr);
      ctx.lineTo(cx + 10, cy - 3 + pr);
      ctx.stroke();
    });
  } else if (mood === "thinking") {
    const ph = t * 0.6;
    glowStroke(ctx, c, 2.5, () => {
      ctx.beginPath();
      ctx.moveTo(cx - 22, cy);
      for (let x = -22; x <= 22; x += 2)
        ctx.lineTo(cx + x, cy + Math.sin((x + ph * 25) * 0.2) * 3.5);
      ctx.stroke();
    });
  } else if (mood === "sleeping") {
    const br = Math.sin(t * 0.5);
    glowStroke(ctx, c, 2.5, () => {
      ctx.beginPath();
      ctx.moveTo(cx - 16, cy + br);
      ctx.quadraticCurveTo(cx, cy + br + 3, cx + 16, cy + br);
      ctx.stroke();
    });
  }
  ctx.restore();
}

// --- FX particle system ---

interface FXParticle {
  x: number;
  y: number;
  vx: number;
  vy: number;
  life: number;
  maxLife: number;
  type: "star" | "tear" | "static" | "bubble" | "zzz";
  color: string;
  char?: string;
  size?: number;
  r?: number;
}

function spawnFX(mood: RetroMood, fxP: FXParticle[]) {
  const c = moodDefs[mood].color;
  if (mood === "waking" && Math.random() < 0.03) {
    fxP.push({
      x: 85 + Math.random() * 60,
      y: 135,
      vx: (Math.random() - 0.5) * 0.5,
      vy: -0.55 - Math.random() * 0.4,
      life: 1,
      maxLife: 85,
      type: "zzz",
      color: c,
      char: ["z", "z", "Z"][Math.floor(Math.random() * 3)],
      size: 9 + Math.random() * 4,
    });
  }
  if (mood === "happy" && Math.random() < 0.06)
    fxP.push({
      x: Math.random() * 200 + 16,
      y: 140,
      vx: (Math.random() - 0.5) * 1.5,
      vy: -1.5 - Math.random() * 1.5,
      life: 1,
      maxLife: 60,
      type: "star",
      color: c,
    });
  if (mood === "sad" && Math.random() < 0.04)
    fxP.push({
      x: 80 + Math.random() * 70,
      y: 60,
      vx: (Math.random() - 0.5) * 0.5,
      vy: 1 + Math.random() * 1.5,
      life: 1,
      maxLife: 55,
      type: "tear",
      color: c,
    });
  if (mood === "panic" && Math.random() < 0.1)
    fxP.push({
      x: Math.random() * 220,
      y: Math.random() * 160,
      vx: (Math.random() - 0.5) * 4,
      vy: (Math.random() - 0.5) * 4,
      life: 1,
      maxLife: 15,
      type: "static",
      color: c,
    });
  if (mood === "thinking" && Math.random() < 0.03)
    fxP.push({
      x: 170 + Math.random() * 30,
      y: 20,
      vx: -0.2,
      vy: -0.4 - Math.random() * 0.4,
      life: 1,
      maxLife: 70,
      type: "bubble",
      color: c,
      r: 3 + Math.random() * 3,
    });
  if (mood === "sleeping" && Math.random() < 0.025)
    fxP.push({
      x: 155 + Math.random() * 30,
      y: 25,
      vx: 0.3,
      vy: -0.5 - Math.random() * 0.5,
      life: 1,
      maxLife: 90,
      type: "zzz",
      color: c,
      char: ["z", "Z", "z"][Math.floor(Math.random() * 3)],
      size: 11,
    });
}

function drawFX(
  ctxF: CanvasRenderingContext2D,
  mood: RetroMood,
  fxP: FXParticle[]
) {
  ctxF.clearRect(0, 0, 232, 174);
  spawnFX(mood, fxP);
  const [r, g, b] = hex2rgb(moodDefs[mood].color);
  for (let i = fxP.length - 1; i >= 0; i--) {
    const p = fxP[i];
    const a = p.life / p.maxLife;
    p.x += p.vx;
    p.y += p.vy;
    p.life--;
    if (p.life <= 0) {
      fxP.splice(i, 1);
      continue;
    }
    ctxF.save();
    ctxF.globalAlpha = a;
    if (p.type === "star") {
      ctxF.shadowColor = p.color;
      ctxF.shadowBlur = 6;
      ctxF.fillStyle = p.color;
      const s = 3 * a;
      ctxF.beginPath();
      for (let j = 0; j < 5; j++) {
        const ang = -Math.PI / 2 + (j * 4 * Math.PI) / 5;
        if (j === 0) ctxF.moveTo(p.x + s * Math.cos(ang), p.y + s * Math.sin(ang));
        else ctxF.lineTo(p.x + s * Math.cos(ang), p.y + s * Math.sin(ang));
      }
      ctxF.closePath();
      ctxF.fill();
    } else if (p.type === "tear") {
      ctxF.shadowColor = p.color;
      ctxF.shadowBlur = 5;
      ctxF.fillStyle = p.color;
      ctxF.beginPath();
      ctxF.ellipse(p.x, p.y, 2, 4 * a, 0, 0, Math.PI * 2);
      ctxF.fill();
    } else if (p.type === "static") {
      ctxF.fillStyle = `rgba(${r},${g},${b},${a * 0.8})`;
      ctxF.fillRect(p.x, p.y, 1.5, 1.5);
    } else if (p.type === "bubble") {
      ctxF.shadowColor = p.color;
      ctxF.shadowBlur = 5;
      ctxF.strokeStyle = p.color;
      ctxF.lineWidth = 1.2;
      ctxF.beginPath();
      ctxF.arc(p.x, p.y, p.r!, 0, Math.PI * 2);
      ctxF.stroke();
    } else if (p.type === "zzz") {
      ctxF.shadowColor = p.color;
      ctxF.shadowBlur = 6;
      ctxF.fillStyle = p.color;
      const sz = (p.size || 11) * (0.6 + a * 0.4);
      ctxF.font = `bold ${sz}px 'Share Tech Mono',monospace`;
      ctxF.fillText(p.char!, p.x, p.y);
    }
    ctxF.restore();
  }
  if (mood === "panic" && Math.random() < 0.15) {
    ctxF.save();
    ctxF.globalAlpha = Math.random() * 0.22;
    ctxF.fillStyle = `rgba(${r},${g},${b},1)`;
    ctxF.fillRect(0, Math.random() * 174, 232, 2 + Math.random() * 7);
    ctxF.restore();
  }
}

// --- Body position helper ---

function getTargetBodyPos(
  mood: RetroMood,
  t: number
): { x: number; y: number; r: number } {
  const anim = moodDefs[mood].bodyAnim;
  const period = bodyPeriods[anim];
  const p = (t % period) / period;
  const raw = bodyKeyframes[anim](p);
  return { x: raw.x || 0, y: raw.y || 0, r: raw.r || 0 };
}

// --- Component ---

const TRANS_DUR = 0.55; // seconds

export default function RetroTVMascot({ mood }: { mood: RetroMood }) {
  const tvBodyRef = useRef<HTMLDivElement>(null);
  const eyeLRef = useRef<HTMLCanvasElement>(null);
  const eyeRRef = useRef<HTMLCanvasElement>(null);
  const mouthRef = useRef<HTMLCanvasElement>(null);
  const fxRef = useRef<HTMLCanvasElement>(null);
  const bgStripesRef = useRef<HTMLDivElement>(null);
  const screenGlowRef = useRef<HTMLDivElement>(null);
  const pdRef = useRef<HTMLDivElement>(null);

  // Mutable state for the animation loop
  const moodStateRef = useRef({
    current: mood,
    prev: mood,
    transitionT: 1,
  });
  const fxPRef = useRef<FXParticle[]>([]);
  const bodyPosRef = useRef({ x: 0, y: 0, r: 0 });
  const tRef = useRef(0);

  // Handle mood changes — update visual styles + trigger transition
  useEffect(() => {
    if (mood === moodStateRef.current.current) return;
    moodStateRef.current.prev = moodStateRef.current.current;
    moodStateRef.current.current = mood;
    moodStateRef.current.transitionT = 0;
    fxPRef.current.length = 0; // clear particles on mood change

    const m = moodDefs[mood];
    if (screenGlowRef.current)
      screenGlowRef.current.style.background = m.bg;
    if (bgStripesRef.current) {
      bgStripesRef.current.style.background = m.stripes;
      bgStripesRef.current.style.backgroundSize = "100% 80px";
    }
    if (pdRef.current) {
      pdRef.current.style.background = m.color;
      pdRef.current.style.boxShadow = `0 0 6px ${m.color}`;
    }
  }, [mood]);

  // Animation loop — runs once on mount, cleaned up on unmount
  useEffect(() => {
    const eyeL = eyeLRef.current;
    const eyeR = eyeRRef.current;
    const mouth = mouthRef.current;
    const fx = fxRef.current;
    if (!eyeL || !eyeR || !mouth || !fx) return;

    const ctxL = eyeL.getContext("2d")!;
    const ctxR = eyeR.getContext("2d")!;
    const ctxM = mouth.getContext("2d")!;
    const ctxF = fx.getContext("2d")!;

    let raf = 0;

    const animate = () => {
      tRef.current += 0.016;
      const t = tRef.current;
      moodStateRef.current.transitionT = Math.min(
        moodStateRef.current.transitionT + 0.016,
        TRANS_DUR
      );
      const p = Math.min(1, moodStateRef.current.transitionT / TRANS_DUR);
      const ep = ease(p);
      const from = moodStateRef.current.prev;
      const to = moodStateRef.current.current;

      // Body lerp — spring towards target
      const target = getTargetBodyPos(to, t);
      bodyPosRef.current.x += (target.x - bodyPosRef.current.x) * 0.08;
      bodyPosRef.current.y += (target.y - bodyPosRef.current.y) * 0.08;
      bodyPosRef.current.r += (target.r - bodyPosRef.current.r) * 0.08;
      if (tvBodyRef.current) {
        const bp = bodyPosRef.current;
        tvBodyRef.current.style.transform = `translate(${bp.x.toFixed(2)}px, ${bp.y.toFixed(2)}px) rotate(${(bp.r * 180 / Math.PI).toFixed(3)}deg)`;
      }

      // Eyes — cross-fade between from and to
      ctxL.clearRect(0, 0, 36, 36);
      ctxR.clearRect(0, 0, 36, 36);
      if (p < 1) {
        drawEyeForMood(ctxL, from, "L", t, 1 - ep);
        drawEyeForMood(ctxR, from, "R", t, 1 - ep);
      }
      drawEyeForMood(ctxL, to, "L", t, ep);
      drawEyeForMood(ctxR, to, "R", t, ep);

      // Mouth cross-fade
      ctxM.clearRect(0, 0, 72, 32);
      if (p < 1) drawMouthForMood(ctxM, from, t, 1 - ep);
      drawMouthForMood(ctxM, to, t, ep);

      // FX
      drawFX(ctxF, to, fxPRef.current);

      raf = requestAnimationFrame(animate);
    };

    // Initialize visual styles for the initial mood
    const m = moodDefs[moodStateRef.current.current];
    if (screenGlowRef.current) screenGlowRef.current.style.background = m.bg;
    if (bgStripesRef.current) {
      bgStripesRef.current.style.background = m.stripes;
      bgStripesRef.current.style.backgroundSize = "100% 80px";
    }
    if (pdRef.current) {
      pdRef.current.style.background = m.color;
      pdRef.current.style.boxShadow = `0 0 6px ${m.color}`;
    }

    raf = requestAnimationFrame(animate);
    return () => cancelAnimationFrame(raf);
  }, []);

  return (
    <div className="retro-tv-mascot">
      <div className="tv-body" ref={tvBodyRef}>
        <div className="tv-shell">
          <div className="tv-ridge" />
          <div className="bezel">
            <div className="screen">
              <div className="bg-stripes" ref={bgStripesRef} />
              <div className="screen-glow" ref={screenGlowRef} />
              <div className="vignette" />
              <div className="scanlines" />
              <div className="face">
                <div className="eyes-row">
                  <canvas ref={eyeLRef} width={36} height={36} />
                  <canvas ref={eyeRRef} width={36} height={36} />
                </div>
                <canvas ref={mouthRef} width={72} height={32} />
              </div>
              <canvas
                ref={fxRef}
                width={232}
                height={174}
                style={{
                  position: "absolute",
                  inset: 0,
                  zIndex: 7,
                  pointerEvents: "none",
                  borderRadius: 7,
                }}
              />
            </div>
          </div>
          <div className="tv-btm">
            <div className="dots">
              <div className="dot on" ref={pdRef} />
              <div className="dot" />
              <div className="dot" />
            </div>
            <div className="cbtns">
              <div className="cb" />
              <div className="cb" />
              <div className="cb" />
              <div className="cb" />
            </div>
          </div>
        </div>
        <div className="stand" />
        <div className="feet">
          <div className="foot" />
          <div className="foot" />
        </div>
      </div>
    </div>
  );
}
