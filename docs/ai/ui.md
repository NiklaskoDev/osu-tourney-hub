# User interface

Goal: looks like a designer made deliberate choices for this product. Not like a generated template.

## Order of authority
1. Existing design system, tokens and components in this repo — reuse, do not restyle.
2. Platform conventions (OS / framework UI guidelines).
3. The rules below.
No design system yet → propose one in the plan (palette, type scale, spacing scale, radius, component list) and get it approved before building screens.

## Do not (typical AI tells)
- Purple/indigo/violet gradients; gradient text; glowing or neon accents.
- Glassmorphism, blurred blobs, decorative background shapes.
- Emoji as icons or in headings, buttons, labels. Sparkle/rocket/magic-wand icons.
- Everything centred; hero + three feature cards + CTA layout by default.
- Every element a rounded card with a shadow; cards inside cards.
- Oversized border radius and padding everywhere; pill buttons by default.
- Many accent colours; coloured badge/pill on everything.
- Animation on everything; hover-scale; entrance fades on scroll.
- Filler copy: "Welcome to…", "Seamlessly", "Effortlessly", "Unlock", "Supercharge", exclamation marks, a subtitle that restates the title.
- Placeholder content left in: lorem ipsum, fake stats, stock avatars, invented testimonials.
- Dark mode with pure black and saturated accents as the default look.

## Do
- **Colour:** neutral base + one accent. Colour carries meaning (action, state), not decoration. Text contrast ≥ 4.5:1 (WCAG AA).
- **Type:** one family (two at most), a fixed scale of 4–6 sizes, hierarchy through size and weight. Body line length 45–75 characters.
- **Spacing:** one scale (e.g. 4/8/12/16/24/32/48). Group by proximity and alignment before reaching for borders or boxes.
- **Layout:** left-aligned by default, a consistent grid, density that fits the task — tools and data screens are compact, not airy.
- **Components:** one style per role (one primary button style, one input style). One primary action per view.
- **Icons:** a single consistent icon set, only where they aid recognition, with text labels for anything non-obvious.
- **Copy:** specific, plain, short. Label buttons with the action ("Save changes", not "Submit"). Sentence case.
- **Motion:** only to explain a state change; 150–250 ms; honour `prefers-reduced-motion`.

## Every screen must handle
- States: loading, empty (with next step), error (what happened + how to recover), success, disabled.
- Long and missing content: truncation, wrapping, zero/one/many items.
- Responsive from 360 px wide upward; touch targets ≥ 44 px.
- Keyboard: logical tab order, visible focus, Esc closes overlays.
- Semantics: real elements (`button`, `label`, headings in order), alt text, labels bound to inputs. No colour-only signals.
- Forms: inline validation next to the field, input preserved on error, destructive actions confirmed or undoable.

## Before reporting UI work as done
Run it and look at it: default, narrow, and one error state. Compare against an existing screen for consistency. If it cannot be run, say so.
