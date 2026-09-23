import { useEffect, useRef } from "react";

/** Rotating 3D point sphere projected to 2D canvas. Zero deps, DPR-aware. */
export default function ParticleSphereAnimation({ points = 420 }: { points?: number }) {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const dpr = Math.min(window.devicePixelRatio, 2);
    let w = 0;
    let h = 0;
    const resize = () => {
      const rect = canvas.getBoundingClientRect();
      w = Math.max(rect.width, 1);
      h = Math.max(rect.height, 1);
      canvas.width = w * dpr;
      canvas.height = h * dpr;
    };
    resize();
    window.addEventListener("resize", resize);

    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const seeds = Array.from({ length: points }, (_, i) => {
      // Fibonacci sphere for even coverage.
      const y = 1 - (i / (points - 1)) * 2;
      const r = Math.sqrt(Math.max(0, 1 - y * y));
      const theta = i * 2.399963;
      return { x: Math.cos(theta) * r, y, z: Math.sin(theta) * r };
    });

    let alive = true;
    let angle = 0;
    const accent = getComputedStyle(document.documentElement).getPropertyValue("--interactive-accent").trim() || "#37c4d6";
    const frame = () => {
      if (!alive) return;
      if (!reduced) angle += 0.0035;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      const R = Math.min(w, h) * 0.36;
      const cx = w / 2;
      const cy = h / 2;
      const cos = Math.cos(angle);
      const sin = Math.sin(angle);
      for (const p of seeds) {
        const x = p.x * cos - p.z * sin;
        const z = p.x * sin + p.z * cos;
        const depth = (z + 1) / 2;
        const sx = cx + x * R;
        const sy = cy + p.y * R * 0.95;
        ctx.globalAlpha = 0.15 + depth * 0.65;
        ctx.fillStyle = accent;
        const s = 0.6 + depth * 1.1;
        ctx.fillRect(sx, sy, s, s);
      }
      ctx.globalAlpha = 1;
      requestAnimationFrame(frame);
    };
    frame();
    return () => {
      alive = false;
      window.removeEventListener("resize", resize);
    };
  }, [points]);

  return <canvas ref={ref} style={{ width: "100%", height: "100%", display: "block" }} aria-hidden="true" />;
}
