import React from "react";
import ParticleSphereAnimation from "./ParticleSphere";

const orbits = [
  {
    size: "w-110 h-110 md:w-180 md:h-180",
    duration: 18,
    chips: [
      { label: "documents", angle: -60 },
      { label: "search", angle: 0 },
      { label: "vectors", angle: 60 },
    ],
  },
  {
    size: "w-150 h-150 md:w-220 md:h-220",
    duration: 24,
    chips: [
      { label: "facts", angle: 0 },
      { label: "graph", angle: -90 },
    ],
  },
  {
    size: "w-180 h-180 md:w-265 md:h-265",
    duration: 30,
    chips: [
      { label: "tags", angle: -60 },
      { label: "memory", angle: 0 },
      { label: "recall", angle: 60 },
    ],
  },
];

export default function MemoryOrbitHero() {
  return (
    <div className="relative w-full h-110 md:h-160 overflow-hidden flex justify-center">
      <style>{`
        @keyframes orbit-cw {
          from { transform: rotate(var(--start-angle)) }
          to   { transform: rotate(calc(var(--start-angle) + 360deg)) }
        }
        @keyframes orbit-ccw {
          from { transform: rotate(var(--start-angle)) }
          to   { transform: rotate(calc(var(--start-angle) - 360deg)) }
        }
        @keyframes counter-cw {
          from { transform: rotate(var(--counter-offset, 0deg)) }
          to   { transform: rotate(calc(var(--counter-offset, 0deg) - 360deg)) }
        }
        @keyframes counter-ccw {
          from { transform: rotate(var(--counter-offset, 0deg)) }
          to   { transform: rotate(calc(var(--counter-offset, 0deg) + 360deg)) }
        }
        @media (prefers-reduced-motion: reduce) {
          .orbit-ring, .orbit-chip { animation: none !important; }
        }
      `}</style>

      {/* Center particle globe */}
      <div className="absolute bottom-0 left-1/2 -translate-x-1/2 translate-y-1/2 aspect-square pointer-events-none w-75 md:w-145 z-10">
        <ParticleSphereAnimation />
      </div>

      {/* Orbiting rings */}
      {orbits.map((orbit, index) => {
        const isCW = index % 2 === 0;
        const orbitAnim = isCW ? "orbit-cw" : "orbit-ccw";
        const counterAnim = isCW ? "counter-cw" : "counter-ccw";

        const allChips = [
          ...orbit.chips,
          ...orbit.chips.map((c) => ({ ...c, angle: c.angle + 180 })),
        ];

        return (
          <div
            key={index}
            className={`absolute bottom-0 left-1/2 -translate-x-1/2 translate-y-1/2 rounded-full border border-border ${orbit.size}`}
          >
            {allChips.map((chip, chipIndex) => (
              <div
                key={chipIndex}
                className="orbit-ring absolute top-0 left-1/2 h-1/2 -ml-8 origin-bottom flex flex-col justify-start items-center"
                style={
                  {
                    "--start-angle": `${chip.angle}deg`,
                    animation: `${orbitAnim} ${orbit.duration}s linear infinite`,
                  } as React.CSSProperties
                }
              >
                <div
                  className="orbit-chip p-3 sm:p-4 border border-border rounded-full bg-background -mt-8 relative z-10"
                  style={
                    {
                      "--counter-offset": `${-chip.angle}deg`,
                      animation: `${counterAnim} ${orbit.duration}s linear infinite`,
                    } as React.CSSProperties
                  }
                >
                  <span className="mono muted whitespace-nowrap">{chip.label}</span>
                </div>
              </div>
            ))}
          </div>
        );
      })}
    </div>
  );
}
