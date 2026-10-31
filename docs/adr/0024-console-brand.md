# ADR 0024: The console in the Getronics brand

- **Status:** Accepted, 2026-10-31; amended 2026-10-31 (decision 8)
- **Decided by:** Amit Kumar (to accept on merge)
- **Implements:** the brand for the operations console and customer portal
  (`apps/web`), within ADR 0014's design rules and ADR 0017's CSP.
  `src/styles/tokens.css`, `src/app/globals.css`, `src/app/layout.tsx`,
  `src/fonts/`, `src/brand/`, `src/app/icon.png`, `src/components/` (`BrandLogo.tsx` new),
  the screens' class names.

## Sources

1. **The Getronics Brand Book, 2026 edition, v1.1** (`Getronics_BrandBook_EN_External_V.1.1.pdf`,
   supplied by Amit Kumar). The primary source: where it and the website differ, the
   book wins. It is not committed.
2. **getronics.com**, read on 2026-10-31: the home page's HTML and its stylesheets
   (`/_next/static/chunks/*.css`). Values only. No asset was copied or hotlinked,
   and the logo was not taken.

## What the brand says

| Element | Value | Source |
|---|---|---|
| Getronics Black | `#000000`: body text, headlines, overlays, icons | Book, colour system |
| Getronics White | `#FFFFFF`: backgrounds, content blocks | Book |
| Getronics Green (primary) | `#57BD37`: highlights, buttons, icons, key accents, "always in subtle presence" | Book |
| Logo Neon Green | `#03E100`: the logo and small UI elements only | Book |
| Mint Green (secondary) | `#A4D9A3`: accent backgrounds, callout boxes | Book |
| Frost Grey (secondary) | `#DEE4E7`: neutral backgrounds, section dividers | Book |
| Typography grey | `#697377`: the only text colour besides black and white | Book, font colour rules |
| Text rules | black on light, white on dark, 4.5:1 minimum, **no green text on white or grey** | Book, WCAG section |
| Headline face | Helvetica, bold and regular mixed | Book, typography |
| Sub-header face | Avenir, Roman and Bold | Book |
| Body face | Segoe UI, regular, 14–18 px | Book |
| Styling | sentence case; all caps only in buttons and short labels; no underline except links | Book |
| Graphic element | the circle, the one shape in the system: green, white, or grey at 20 % | Book, visual elements |
| Site greens and greys | `--getronics-green: #02e600`, `--getronics-light-grey: #f6f6f6`, `--getronics-cool-grey: #d3dce0`, `--getronics-cool-grey-dark: #697377` | getronics.com CSS |
| Site faces | `helvetica-lt-pro` (titles), `segoe-ui` (body), both self-hosted webfonts | getronics.com CSS |
| Site shape | radii 0.4–1.6 rem at a 10 px root (4–16 px); black buttons with a white chip, green on hover; a green dot before eyebrow labels | getronics.com CSS |
| Tone | "reliability over hype", "calm confidence under pressure", professional with a human touch | Book, brand character |

The site's green (`#02e600`) is the book's Logo Neon Green (`#03E100`) to within
rounding. This ADR uses the book's values.

## Decisions

1. **All brand values live in `tokens.css`, ADR 0014's one token file.** A rebrand is
   still a one-file change for colour, type and shape. New tokens: `line-subtle`
   (Frost Grey hairlines), `accent-hover`, `link`, `selected` (a Mint tint),
   `brand` (Logo Neon Green), the `chrome-*` set for the header, and two
   shadows. `globals.css` adds a few shared utilities (`btn`, `btn-primary`,
   `btn-secondary`, `field`, `card`, `link`, `section-title`). They set shape and
   type only and read every colour from a token.

2. **How the colours are used.**
   - **Base layers.** A white header band (decision 8) and white cards on a Frost Grey tint
     canvas (`#F5F7F8`).
   - **Getronics Green.** Primary buttons, green with black text (8.7:1). White on
     this green is 2.4:1, so it is never used.
   - **Neon green.** Only the small circle that marks the current module, the logo
     slot's dot, and the header's focus ring.
   - **Mint.** A tint marks the current navigation item.
   - **Links.** Black and underlined in light mode, Mint in dark. Green text never
     appears on a light background.
   - **Departure 1.** Secondary text is `#5F686C`, one step darker than the book's
     `#697377`. `#697377` reaches only 4.3:1 on the canvas and raised tints, which
     fails AA.
   - **Departure 2.** Tints of Frost Grey and Mint are used for the canvas and
     selection fills, because the full-strength colours are too heavy behind dense
     text.

3. **Health bands are unchanged in hue and still AA in both themes.** They remain
   green, amber, orange, red and grey, always with a word and a dot (WCAG 1.4.1).
   - Critical moved slightly towards crimson (`#B0102A` light, `#FF8F9D` dark), so it
     reads apart from at-risk orange.
   - Unknown follows the new greys.
   - Every band's text is at least 5.3:1 on its fill in light mode and 6.1:1 in dark.
   - The brand green appears only on buttons and marks, never as a band, so "green
     button" and "healthy" do not compete.
   - Modelled values keep their dashed purple marker.

4. **Typefaces: the brand's where installed, open stand-ins otherwise.** Helvetica,
   Avenir and Segoe UI are licensed fonts and cannot be served from this app.
   `tokens.css` names each brand face first, so it is used where the machine has
   it. Segoe UI is on every Windows machine, so most Getronics staff see the real
   body face. Behind each brand face is an open stand-in, self-hosted through
   `next/font/local` from `src/fonts/`, with its OFL licence beside it. The CSP's
   `font-src 'self'` is unchanged.

   | Brand face | Role | Stand-in | Why |
   |---|---|---|---|
   | Helvetica | page titles, figures | **Arimo** (OFL) | metric-compatible with Arial and so with Helvetica's widths; a neutral grotesque |
   | Avenir | section headings | **Nunito Sans** (OFL) | the closest open geometric-humanist sans to Avenir |
   | Segoe UI | body, controls | **Open Sans** (OFL) | the usual open humanist stand-in for Segoe UI |

   Microsoft's Selawik, which is built to match Segoe UI's metrics, was tried and
   dropped: Chromium rendered it with broken letter spacing ("RIT M0040098",
   "Perf ormance"). All three files are the Latin subset; anything outside it falls
   back to the system font.

5. **Type scale: dense rather than marketing-sized.** The book's sizes are for
   documents and the web (H1 40–60 px, H2 28–36 px, body 14–18 px). A console that
   shows tables all day needs smaller sizes, so it uses:
   - H1 28 px in the headline face;
   - section headings 17 px in the sub-header face;
   - body and tables 14 px;
   - labels and provenance 12 px.

   Buttons are all caps with slight tracking, as the book sets for buttons. Headings
   and labels are in sentence case.

6. **The logo is the brand book's own approved artwork.**
   - The header and the sign-in panel are black, so they use the white version,
     the book's version for dark backgrounds. It is taken unmodified from the
     book's logo page as a PNG (`src/brand/getronics-logo-white.png`). Nothing was
     taken from getronics.com.
   - Its transparent margin is kept as the clear space. At 40 px tall the artwork
     is about 86 px wide, above the book's 68 px screen minimum. Nothing is
     stretched, recoloured or given an effect.
   - The favicon (`src/app/icon.png`) is the green "g" brandmark, centred
     unaltered on a transparent square. The book names the brandmark for
     favicons.
   - The logo is imported, so it is bundled and served from `/_next/static`. The
     standalone container does not ship `public/`, and `img-src 'self'` covers
     the file.
   - If brand supplies vector (SVG) masters, they replace these files one for
     one.

7. **Behaviour is unchanged.** The following are not touched:
   - role-driven navigation and its permission filter;
   - the data each screen shows, provenance and as-of labels, and "not available"
     for a figure the role cannot see;
   - API calls, auth and the CSP.

   The circle in front of each navigation item is decorative (`aria-hidden`).
   `aria-current` still marks the current page.

8. **Amendment: a white header, and Getronics backgrounds outside the data screens.**
   Amit Kumar pointed to Getronics' internal service dashboard (gboard,
   mon.getronics.com) as the look to match. That look is a white header with the
   full-colour logo, a light sidebar, and white cards with coloured edges, with no
   imagery on the data screens.
   - **The header is white** in light mode, with the full-colour (green) logo: the
     book's default on light backgrounds. In dark mode it is the dark surface with
     the white logo. The `chrome-*` tokens carry both, and a `dark:` variant in
     `globals.css` follows the same rule as `tokens.css`. The logo is 44 px tall,
     so the green artwork is about 74 px wide, above the 68 px minimum.
   - **Backgrounds are Getronics' official Teams backgrounds**, supplied by Amit
     Kumar from marketing's brand assets (`Brand assets/_Backgrounds+Wallpapers/
     _TeamsBackground` on the G-home SharePoint). Three are used:
     - **sign-in:** the glass building entrance in light mode, the sunset office
       in dark mode, full screen, with the form in a white card on the left;
     - **the signed-out, no-access and could-not-reach states:** the white atrium,
       beside the message.

     Each image carries the full-colour logo in its top-right corner. The book
     forbids cropping the logo, so every crop is anchored top-right
     (`object-right-top`): an image loses its left side first, never that corner.
     Nothing is placed over the logo, and no second logo is added to an image.
     No text is set on the photographs; it all sits in solid cards.
   - **Data screens carry no imagery.** The book forbids text over busy
     backgrounds, and dashboards need the contrast.
   - **Files and handling.** The images are in `src/brand/` as WebP, re-encoded
     at their original 1920 × 1080, not cropped or retouched. They are bundled
     like the logo, so `img-src 'self'` covers them. They are decorative, with
     empty `alt`.

## Consequences

- Selectors that use roles and names are unaffected. CSS upper-casing changes how a
  button looks, not its accessible name.
- On the data screens, the circle and the Mint selection are the only decoration.
  There is no icon set: the book allows only its own icon library, and we do not
  have it. Background images appear only as decision 8 describes.
- Open:
  - brand team confirmation that the open stand-in fonts are acceptable;
  - vector logo masters, if brand wants them to replace the PNGs.

  A licensed webfont kit, if Getronics has one for internal applications, would
  replace `src/fonts/` and leave the rest unchanged.
