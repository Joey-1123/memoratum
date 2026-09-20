import { useEffect, useRef } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import type { GraphFact } from "../types";

function colorFor(text: string): number {
  let h = 0;
  for (let i = 0; i < text.length; i++) h = (h * 31 + text.charCodeAt(i)) >>> 0;
  return new THREE.Color(`hsl(${h % 360}, 60%, 55%)`).getHex();
}

export function ThreeGraphView({
  facts,
  onSelect,
}: {
  facts: GraphFact[];
  onSelect: (node: string, facts: GraphFact[]) => void;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const selectRef = useRef(onSelect);
  selectRef.current = onSelect;

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const names = [...new Set(facts.flatMap((f) => [f.subject, f.object]))];
    if (!names.length) return;
    const byNode = new Map<string, GraphFact[]>();
    for (const f of facts) {
      for (const n of [f.subject, f.object]) {
        const list = byNode.get(n) ?? [];
        list.push(f);
        byNode.set(n, list);
      }
    }

    const scene = new THREE.Scene();
    scene.background = new THREE.Color("#0a0e14");
    const camera = new THREE.PerspectiveCamera(60, el.clientWidth / Math.max(el.clientHeight, 1), 0.1, 2000);
    camera.position.set(0, 8, 26);
    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(el.clientWidth, el.clientHeight || 480);
    el.appendChild(renderer.domElement);

    scene.add(new THREE.AmbientLight(0xffffff, 0.7));
    const key = new THREE.DirectionalLight(0xffffff, 1.2);
    key.position.set(5, 10, 7);
    scene.add(key);

    const positions = new Map<string, THREE.Vector3>();
    names.forEach((name, i) => {
      const t = (i / Math.max(names.length, 1)) * Math.PI * 2;
      const r = 6 + (i % 5);
      positions.set(name, new THREE.Vector3(Math.cos(t) * r, ((i * 37) % 11) - 5, Math.sin(t) * r));
    });

    const sphere = new THREE.SphereGeometry(0.16, 12, 12);
    const material = new THREE.MeshStandardMaterial({ roughness: 0.4, metalness: 0.2 });
    const mesh = new THREE.InstancedMesh(sphere, material, names.length);
    const matrix = new THREE.Matrix4();
    const color = new THREE.Color();
    names.forEach((name, i) => {
      const p = positions.get(name);
      if (!p) return;
      matrix.setPosition(p.x, p.y, p.z);
      mesh.setMatrixAt(i, matrix);
      mesh.setColorAt(i, color.setHex(colorFor(name)));
    });
    mesh.instanceMatrix.needsUpdate = true;
    if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
    scene.add(mesh);

    const edgePos = new Float32Array(facts.length * 6);
    facts.forEach((f, i) => {
      const a = positions.get(f.subject);
      const b = positions.get(f.object);
      if (!a || !b) return;
      edgePos.set([a.x, a.y, a.z, b.x, b.y, b.z], i * 6);
    });
    const edgeGeo = new THREE.BufferGeometry();
    edgeGeo.setAttribute("position", new THREE.BufferAttribute(edgePos, 3));
    scene.add(new THREE.LineSegments(edgeGeo, new THREE.LineBasicMaterial({ color: 0x2b3648, transparent: true, opacity: 0.6 })));

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.06;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    controls.autoRotate = !reduced;
    controls.autoRotateSpeed = 0.6;

    const raycaster = new THREE.Raycaster();
    const pointer = new THREE.Vector2();
    const onClick = (e: MouseEvent) => {
      const rect = renderer.domElement.getBoundingClientRect();
      pointer.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
      pointer.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
      raycaster.setFromCamera(pointer, camera);
      const hit = raycaster.intersectObject(mesh)[0];
      if (hit?.instanceId != null) {
        const name = names[hit.instanceId];
        selectRef.current(name, byNode.get(name) ?? []);
      }
    };
    renderer.domElement.addEventListener("click", onClick);

    let alive = true;
    const clock = new THREE.Clock();
    const animate = () => {
      if (!alive) return;
      requestAnimationFrame(animate);
      controls.update(clock.getDelta());
      renderer.render(scene, camera);
    };
    animate();

    const onResize = () => {
      camera.aspect = el.clientWidth / Math.max(el.clientHeight, 1);
      camera.updateProjectionMatrix();
      renderer.setSize(el.clientWidth, el.clientHeight || 480);
    };
    window.addEventListener("resize", onResize);
    return () => {
      alive = false;
      window.removeEventListener("resize", onResize);
      renderer.domElement.removeEventListener("click", onClick);
      controls.dispose();
      scene.traverse((o) => {
        const m = o as THREE.Mesh;
        if (m.geometry) m.geometry.dispose();
        const mat = m.material as THREE.Material | THREE.Material[] | undefined;
        if (Array.isArray(mat)) mat.forEach((x) => x.dispose());
        else mat?.dispose();
      });
      renderer.dispose();
      renderer.domElement.remove();
    };
  }, [facts]);

  if (facts.length === 0) {
    return (
      <div className="card" role="status">
        <h3>No facts in this tag</h3>
      </div>
    );
  }
  return <div ref={containerRef} style={{ height: "70vh" }} role="application" aria-label="3D memory graph. Click a node to inspect it." />;
}
