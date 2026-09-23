import type { ReactNode } from "react";

function Frame({ label, children }: { label: string; children: ReactNode }) {
  const decorative = label === "";
  return (
    <svg
      width="20"
      height="20"
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      role={decorative ? undefined : "img"}
      aria-label={decorative ? undefined : label}
      aria-hidden={decorative || undefined}
    >
      {children}
    </svg>
  );
}

export function TagsIcon({ label }: { label: string }) {
  return (
    <Frame label={label}>
      <path d="M3 5.5A1.5 1.5 0 0 1 4.5 4h4l6 6-4.5 4.5a1.5 1.5 0 0 1-2 0z" />
      <circle cx="8" cy="8" r="1" fill="currentColor" stroke="none" />
    </Frame>
  );
}

export function GraphIcon({ label }: { label: string }) {
  return (
    <Frame label={label}>
      <circle cx="6" cy="6" r="2.2" />
      <circle cx="14" cy="14" r="2.2" />
      <circle cx="14.5" cy="5.5" r="1.4" />
      <path d="M7.8 7.3l4.6 5M7.9 6.2l4.9-.5" />
    </Frame>
  );
}

export function SearchIcon({ label }: { label: string }) {
  return (
    <Frame label={label}>
      <circle cx="9" cy="9" r="5" />
      <path d="M13 13l3.5 3.5" />
    </Frame>
  );
}

export function ImportIcon({ label }: { label: string }) {
  return (
    <Frame label={label}>
      <path d="M10 3v9M6.5 8.5L10 12l3.5-3.5M4 16.5h12" />
    </Frame>
  );
}

export function ExportIcon({ label }: { label: string }) {
  return (
    <Frame label={label}>
      <path d="M10 12V3M6.5 6.5L10 3l3.5 3.5M4 16.5h12" />
    </Frame>
  );
}
