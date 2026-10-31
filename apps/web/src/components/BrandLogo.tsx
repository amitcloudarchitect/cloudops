// The Getronics logo (ADR 0024).
//
// The approved artwork, taken unmodified from the Brand Book v1.1's logo page: the
// full-colour (green) logo on light backgrounds, the white one on dark backgrounds. Each file's transparent margin is its clear space. The book's
// rules: no stretching, recolouring, shadow or glow, and at least 68 px wide on
// screen. At 44 px tall the green artwork is about 74 px wide, so no size here may go smaller.
//
// Imported, not put in public/: the standalone build ships only what is bundled,
// and the files are then served from /_next/static, same-origin (img-src 'self').

import green from "@/brand/getronics-logo-green.png";
import white from "@/brand/getronics-logo-white.png";

const SIZE = { header: "h-11" } as const;

/** Green in light mode, white in dark (the header). */
export function BrandLogo({ size = "header" }: { size?: keyof typeof SIZE }) {
  // Plain imgs: static files need no optimiser, and the CSP allows only this origin.
  // The hidden one is display:none, so assistive technology reads the name once.
  const img = (src: typeof green, extra: string) => (
    <img src={src.src} width={src.width} height={src.height} alt="Getronics" className={`${SIZE[size]} w-auto ${extra}`} />
  );
  return (
    <>
      {img(green, "dark:hidden")}
      {img(white, "hidden dark:inline")}
    </>
  );
}
