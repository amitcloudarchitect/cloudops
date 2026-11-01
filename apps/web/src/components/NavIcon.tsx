// The navigation's icons: simple line drawings, inline so they need no icon font,
// CDN or CSP change. They take the text colour (currentColor) and are always
// decorative: the label beside them, or the link's accessible name in the rail,
// says what they are (ADR 0024 amendment, ADR 0032 decision 6).

const PATHS = {
  // Areas
  layers: "M12 3 3 8l9 5 9-5-9-5Z M3 13l9 5 9-5 M3 17.5l9 5 9-5",
  shield: "M12 3 4.5 6v5.5c0 4.6 3.2 8.4 7.5 9.5 4.3-1.1 7.5-4.9 7.5-9.5V6L12 3Z",
  coins: "M4 7c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3Z M4 7v5c0 1.7 3.6 3 8 3s8-1.3 8-3V7 M4 12v5c0 1.7 3.6 3 8 3s8-1.3 8-3v-5",
  clipboard: "M9 4h6v3H9z M9 5.5H6.5v15h11v-15H15 M9 12l2 2 4-4",
  book: "M4 5.5C4 4.7 4.7 4 5.5 4H11v16H5.5c-.8 0-1.5-.7-1.5-1.5v-13Z M20 5.5c0-.8-.7-1.5-1.5-1.5H13v16h5.5c.8 0 1.5-.7 1.5-1.5v-13Z",
  gear: "M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6Z M12 2.5v3 M12 18.5v3 M2.5 12h3 M18.5 12h3 M5.3 5.3l2.1 2.1 M16.6 16.6l2.1 2.1 M5.3 18.7l2.1-2.1 M16.6 7.4l2.1-2.1",
  activity: "M3 12h4l2.5-6 5 12 2.5-6h4",
  // Modules
  home: "M3.5 11 12 4l8.5 7 M6 9.5V20h12V9.5 M10 20v-5h4v5",
  queue: "M4 6h16 M4 12h16 M4 18h10",
  server: "M4 4h16v6H4z M4 14h16v6H4z M7.5 7h.01 M7.5 17h.01",
  approve: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Z M8 12.5l2.5 2.5L16 9.5",
  gauge: "M4 17a8 8 0 1 1 16 0 M12 17l4-5",
  building: "M5 21V4h9v17 M14 9h5v12 M8 8h3 M8 12h3 M8 16h3 M3 21h18",
  radar: "M12 3a9 9 0 1 0 9 9 M12 7a5 5 0 1 0 5 5 M12 12l6-6",
  wallet: "M4 7h14a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H4V7Z M4 7l11-3v3 M16 13.5h.01",
  percent: "M5 19 19 5 M7 5a2 2 0 1 0 0 4 2 2 0 0 0 0-4Z M17 15a2 2 0 1 0 0 4 2 2 0 0 0 0-4Z",
  tag: "M3 12V4h8l9 9-8 8-9-9Z M7.5 7.5h.01",
  trend: "M3 17l6-6 4 4 8-8 M15 7h6v6",
  file: "M6 3h8l4 4v14H6V3Z M14 3v4h4 M9 12h6 M9 16h6",
  search: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14Z M16 16l4.5 4.5",
  users: "M9 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z M2.5 20c0-3.6 2.9-6 6.5-6s6.5 2.4 6.5 6 M16 4.5a3.5 3.5 0 0 1 0 6.5 M18 14.5c2 .7 3.5 2.6 3.5 5.5",
  cloud: "M7 19a4.5 4.5 0 0 1-.5-9A6 6 0 0 1 18 9.5a4.5 4.5 0 0 1-.5 9.5H7Z",
  door: "M5 21V4h10v17 M15 6h4v15 M3 21h18 M11.5 12.5h.01",
  link: "M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1 M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1",
  plug: "M9 3v5 M15 3v5 M6 8h12v3a6 6 0 0 1-12 0V8Z M12 17v4",
  sliders: "M4 6h10 M18 6h2 M4 12h4 M12 12h8 M4 18h12 M18 18h2 M16 4v4 M10 10v4 M16 16v4",
  chevron: "M9 6l6 6-6 6",
  rail: "M4 4h16v16H4z M9 4v16 M15 10l-2 2 2 2",
} as const;

export type IconName = keyof typeof PATHS;

export function NavIcon({ name, className = "size-5" }: { name: IconName; className?: string }) {
  return (
    <svg
      aria-hidden="true"
      focusable="false"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`shrink-0 ${className}`}
    >
      <path d={PATHS[name]} />
    </svg>
  );
}
