// The Getronics logo (ADR 0024).
//
// The approved white logo, the brand book's version for dark backgrounds (the
// header and the sign-in panel are black), taken unmodified from the Brand Book
// v1.1's logo page. Its transparent margin is the clear space. The book's rules: no
// stretching, recolouring, shadow or glow, and at least 68 px wide on screen. At
// 40 px tall the artwork is about 86 px wide, so no size here may go smaller.
//
// Imported, not put in public/: the standalone build ships only what is bundled,
// and the file is then served from /_next/static, same-origin (img-src 'self').

import logo from "@/brand/getronics-logo-white.png";

const SIZE = { header: "h-10", hero: "h-16" } as const;

export function BrandLogo({ size = "header" }: { size?: keyof typeof SIZE }) {
  // A plain img: a static file needs no optimiser, and the CSP allows only this origin.
  return <img src={logo.src} width={logo.width} height={logo.height} alt="Getronics" className={`${SIZE[size]} w-auto`} />;
}
