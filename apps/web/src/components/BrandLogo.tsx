// The Getronics logo slot (ADR 0024).
//
// PLACEHOLDER: no approved logo file is in the repository yet, and the logo is not
// to be copied from getronics.com. When brand supplies one, put the white version
// (the brand book's version for dark backgrounds; the header is black) at
// public/brand/getronics-logo-white.svg and set LOGO_SRC to its path. The brand
// book's rules: the approved file unmodified, no shadow or glow, clear space equal
// to the height of the "G" on every side, at least 68 px wide on screen.

const LOGO_SRC: string | null = null;

export function BrandLogo() {
  if (LOGO_SRC) {
    // A plain img: the file is static, same-origin (img-src 'self'), and needs no optimiser.
    return <img src={LOGO_SRC} alt="Getronics" className="h-6 w-auto min-w-[68px]" />;
  }
  return (
    <span
      data-logo-slot="placeholder"
      title="Logo placeholder: awaiting the approved Getronics logo file"
      className="inline-flex h-7 min-w-[88px] items-center justify-center gap-1.5 rounded-control border border-dashed border-chrome-muted px-2 text-[0.625rem] font-bold uppercase tracking-widest text-chrome-muted"
    >
      <span aria-hidden="true" className="size-2 rounded-full bg-brand" />
      Getronics
      <span className="sr-only"> (logo placeholder)</span>
    </span>
  );
}
