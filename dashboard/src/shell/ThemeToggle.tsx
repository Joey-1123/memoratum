import { useEffect, useState } from "react";

type Theme = "theme-dark" | "theme-light";

function initial(): Theme {
  return localStorage.getItem("memoratum_theme") === "theme-light" ? "theme-light" : "theme-dark";
}

export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(initial);

  useEffect(() => {
    document.documentElement.classList.remove("theme-dark", "theme-light");
    document.documentElement.classList.add(theme);
    localStorage.setItem("memoratum_theme", theme);
  }, [theme]);

  const dark = theme === "theme-dark";
  return (
    <button
      onClick={() => setTheme(dark ? "theme-light" : "theme-dark")}
      aria-label={dark ? "Switch to light theme" : "Switch to dark theme"}
      title="Toggle theme"
    >
      <svg
        width="20"
        height="20"
        viewBox="0 0 20 20"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        aria-hidden="true"
      >
        {dark ? (
          <path d="M15.5 11.5A6.5 6.5 0 0 1 8.5 4.5a6.5 6.5 0 1 0 7 7z" />
        ) : (
          <>
            <circle cx="10" cy="10" r="3.5" />
            <path d="M10 2.5v2M10 15.5v2M2.5 10h2M15.5 10h2M4.7 4.7l1.4 1.4M13.9 13.9l1.4 1.4M15.3 4.7l-1.4 1.4M6.1 13.9l-1.4 1.4" />
          </>
        )}
      </svg>
    </button>
  );
}
