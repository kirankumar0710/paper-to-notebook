---
name: paper-notebook-ui-design
description: UI design guidelines for the Paper-to-Notebook dashboard (api/static). Use whenever adding, changing or restyling a dashboard panel, its HTML/CSS/JS, colours, spacing or layout, so every panel looks like part of the same product.
---

# Paper-to-Notebook UI Design Guidelines

## Where this lives in the code

- `api/static/base.css` defines the palette, spacing and shape as CSS
  tokens (`--bg`, `--card`, `--border`, `--accent`, `--accent-hover`,
  `--text`, `--muted`, `--subtle`, `--ok`, `--warn`, `--error`, `--radius`,
  `--radius-sm`, `--pad`, `--gap`, `--font`, `--mono`, `--surface`, the
  sequence scale `--seq-1`…`--seq-5`, and the section colours
  `--tint-<section>` / `--line-<section>`), the dashboard grid, which
  section colour each panel uses (`--panel-bg` / `--panel-line`), the
  shared panel frame (`section.panel`), the panel header (`.panel-head`,
  `.panel-head h2`, `.panel-actions`) and shared controls (`button.link`).
- Inside a panel, content surfaces (tiles, logs, inputs, drop zone) use
  `var(--surface)` with a `1px solid var(--panel-line)` border, and
  dividers use `var(--panel-line)`, so they follow the panel's colour.
- Each panel is `api/static/panels/<name>/<name>.html|.css|.js`. Panel CSS
  must use the tokens from `base.css`; do not add literal colours, radii or
  per-panel header styles. A panel's section colour is set only in
  `base.css`, never in its own CSS.
- Layout: two tabbed panels side by side (`section.panel.tabbed`).
  **Paper** (left, 1/3, blue): tabs Upload (default) | Download |
  Configuration. **Activity** (right, 2/3, violet): tabs LLM output
  (default) | MCP log | Metrics. On a desktop window at least 1001px wide
  and 640px tall they fill the window below the header and each tab
  scrolls inside, so the page itself never scrolls. Below 1000px they
  stack (Paper first) and the page scrolls.
- A new view is usually a new tab: add its folder (`<name>.html` is a
  `div.tab-panel` with `role="tabpanel"`), a `<link>` for its CSS in
  `index.html`, a tab button plus an include line in `paper.html` or
  `activity.html`, and wire it in `app.js`. A whole new panel also needs a
  grid area in `base.css` and a section colour (see section 4).
- Run check is not in the grid. It is a side drawer (`section.panel
  .drawer`, fixed over the right edge, with the same panel frame and its
  own section colour) used only when the user ticks "Check that the
  notebook runs". It slides in when a check starts, leaves the panels on
  the left visible and usable (no backdrop), and collapses to a vertical
  tab on the right edge (Collapse at the top-right, or Escape). On phones
  it opens full width. `--overlay-shadow` is the only shadow in the
  design, and only for things that float over the page.
- Check changes visually at desktop, tablet and phone widths before
  finishing, and run `pytest` (the page tests check the panels render).
  At desktop size (1440×900), also confirm the page does not scroll (see
  section 7 and acceptance criterion 10), and click through a conversion:
  a script error in `app.js` only shows up in the browser, not in
  `pytest`.

## 1. Objective

Establish a consistent, clean, modern SaaS dashboard UI for the
Paper-to-Notebook application.

The application should have a professional appearance, with clearly
separated functional panels, consistent spacing, typography, colors, and
alignment.

## 2. Global Design Principles

- Use a pure white page background (`#FFFFFF`).
- Give each functional panel its own **soft section colour**: a pale tint
  for the background and a matching light border, following common
  dashboard practice (see "Section colours" in section 4). Colours must be
  soft pastels, never bright or saturated.
- Content inside a panel (cards, logs, inputs) sits on white.
- Every major functional section must be enclosed in an individual rounded
  rectangular panel.
- Maintain consistent panel borders, corner radius, padding, and spacing.
- Use a clean, minimal, professional SaaS dashboard appearance.
- Avoid unnecessary decorative elements, gradients, shadows, and excessive
  colors.
- Maintain visual consistency across all sections.

## 3. Panel Design Standards

Every functional section must follow the same panel structure.

### Panel appearance

- Background: the panel's section tint (`--panel-bg`).
- Border: `1px solid` the panel's section border (`--panel-line`).
- Border radius: `10px` to `12px` (implemented: `--radius: 12px`)
- Internal padding: `12px` to `16px` (implemented: `--pad: 16px`)
- Consistent spacing between panels (implemented: `--gap: 16px`).
- Avoid heavy shadows.
- All panels must have consistent styling: only the section colour
  differs between panels.

### Panel heading

- In the two tabbed panels the tab bar is the heading (see "Tabs" in
  section 5); it follows the same type rules.
- Position: Top-left corner.
- Font weight: Bold.
- Font size: 12px to 13px (implemented: 13px).
- Text transformation: Uppercase.
- Color: `#1E293B`.
- Maintain consistent spacing below the heading.
- No number badges or icons before the title.

### Panel actions

- If a panel has an action such as Refresh, Clear, Settings, or Download:
  - Position it at the top-right corner (inside `.panel-head`, grouped in
    `.panel-actions` when there is more than one).
  - Keep it aligned horizontally with the panel heading.
  - Use a subtle blue text/button style (`button.link`).
  - Avoid oversized buttons.
  - Use consistent styling across all panels.

## 4. Color Palette

| Purpose | Color | Token |
|---|---|---|
| Page background | #FFFFFF | `--bg` |
| Content inside panels | #FFFFFF | `--surface` |
| Default panel background | #FFFFFF | `--card` |
| Default border | #E2E8F0 | `--border` |
| Primary accent | #2563EB | `--accent` |
| Primary hover | #1D4ED8 | `--accent-hover` |
| Main text | #1E293B | `--text` |
| Secondary text | #64748B | `--muted` |
| Success | #16A34A | `--ok` |
| Warning | #D97706 | `--warn` |
| Error | #DC2626 | `--error` |
| Muted background | #F1F5F9 | `--subtle` |

### Section colours

Each panel has a soft tint (Tailwind 50 shade) and a matching border
(100–200 shade). The hue follows what the section is for, as is common in
dashboards:

| Section | Hue | Why | Background | Border | Tokens |
|---|---|---|---|---|---|
| Paper (Upload, Download, Configuration tabs) | Blue | input, the primary action | #EFF6FF | #BFDBFE | `--tint-paper` / `--line-paper` |
| Activity (LLM output, MCP log, Metrics tabs) | Violet | the AI model at work | #F5F3FF | #DDD6FE | `--tint-activity` / `--line-activity` |
| Run check | Sky | test runs of the notebook | #F0F9FF | #BAE6FD | `--tint-check` / `--line-check` |

Rules:
- Keep tints soft: a 50 shade for the background and a 100–200 shade for
  the border. Never use saturated colours for panel backgrounds.
- A section colour identifies the section only. It never means status:
  green, amber and red text, dots and chips still mean success, warning
  and error.
- Headings, text, actions and buttons look the same in every panel; only
  the tint and border change.
- A new panel picks an unused soft hue that fits its purpose, defined as a
  new `--tint-*` / `--line-*` pair in `base.css`.

### Interaction and status colours

Use blue as the primary interactive color.

Use green, amber, and red only to communicate status (text, dots, chips),
not for panel backgrounds other than the section tints above.

Do not introduce additional colors without a functional reason. (The one
exception in use: the Metrics stage bar is a grey-to-blue scale in stage
order, tokens `--seq-1`…`--seq-5` plus `--accent` and `--accent-hover`,
with amber only for "retrying" and "repairing", the stages that recover
from a problem. Use the scale only for ordered stages.)

## 5. Application Panel Structure

Apply the common panel design to the following sections. They are tabs
of two panels: Paper (Upload, Download, Configuration) and Activity (LLM
output, MCP log, Metrics).

### Tabs
- The tab bar replaces the panel title and uses the title's type style
  (13px, bold, uppercase). The selected tab is in `--text` with an
  `--accent` underline on the header's `--panel-line` rule; the others
  are `--muted`.
- Tabs follow the ARIA tabs pattern (`role="tab"`, `aria-selected`,
  arrow keys, Home, End), via `initTabs()` in `shared.js`.
- A tab's own actions (Refresh, Live, Clear) sit at the top-right and show
  only while that tab is selected (`data-tab-for="<tab id>"`).
- Badges: a count in `--accent` (new MCP events while that tab is not
  shown) or a "!" in `--error` (a configuration problem).
- Switching by itself is kept to clear moments: Download is selected when
  a notebook is ready, LLM output when a conversion starts, and
  Configuration on the first load if something is missing. New MCP events
  only badge their tab.

### Configuration (Paper tab)
- Display provider, model, API key status, save folder, and generation
  options.
- Keep labels and values aligned.
- Use clear status indicators.
- Provide Refresh at the top-right when applicable.

### Upload Paper / Specify Link (Paper tab, default)
- Provide a PDF upload/drop area.
- Provide a separate arXiv URL or ID input.
- Notebook depth is a three-way segmented switch (Beginner / Practitioner
  / Expert): segments on `--surface` divided by `--panel-line`, the
  selected one filled with `--accent`, and a hint line below that
  explains the choice.
- Make the primary Convert to Notebook button visually prominent.
- Clearly communicate upload and processing states.

### Download (Paper tab)
- Display the generated notebook information.
- Provide relevant download/open actions.
- Show an appropriate empty state when no notebook is available.
- Provide Refresh at the top-right.

### LLM output (Activity tab, default)
- Display generated explanations and notebook-generation output.
- Support readable text formatting.
- Clearly distinguish loading, completed, and error states.
- The activity log grows to fill the tab.

### MCP log (Activity tab)
- Display MCP server activity in chronological order.
- Show server connection status.
- Display tool calls, arguments, execution status, and results where
  appropriate.
- Provide Clear at the top-right.
- Use readable timestamps and subtle status indicators.
- Support automatic polling without disrupting the user's interaction.

### Run check (side drawer)
- Used only while the run check is used; drawer and tab are hidden
  otherwise. Collapse sits at the top-right like other panel actions; the
  collapsed tab shows a status dot in the status colours.
- One collapsible block per run, cell by cell: a status dot (grey
  pending, blue running, green passed, red failed), the cell's first line
  of code, its time, and its output or error in a collapsible box.
- The model's fixes appear between runs with an amber (or red if the
  repair failed) left rule.

### Metrics (Activity tab)
- Tokens & cost and Time are always shown;
  Paper, Notebook and Since the server started are under a "More details"
  `<details>` that remembers whether it was open.
- Word values (Depth, Run check, GPU) use `.v.word`, smaller than
  numbers, so they fit narrow tiles without breaking mid-word.
- Display token usage, cost, execution time, PDF information, notebook
  information, and GPU information.
- Organize related metrics into logical groups.
- Use consistent metric cards.
- Ensure numbers and labels are easy to scan.

## 6. Typography and Spacing

- Use a modern sans-serif font: Inter, falling back to the system font
  (`--font`).
- Use consistent font sizes and weights.
- Maintain consistent spacing between headings, content, and controls.
- Align panel headings and actions.
- Avoid cramped content and unnecessary whitespace.

## 7. Responsive Design

- **Desktop: everything fits one screen.** On a laptop window (checked at
  1440×900) the whole dashboard is visible without scrolling the page:
  the inputs (Upload, depth, run check) and the progress (LLM output) are
  on screen together. Long content scrolls inside its tab, never the
  page. This is a hard rule:
  - Add new views as a **tab** of Paper or Activity, or as a drawer or
    overlay like Run check. Do not add a grid row or a panel below the
    two panels.
  - Keep tab content compact enough to be useful at 900px tall; put
    secondary details in a collapsible `<details>` (as Metrics does).
  - The fixed-height mode applies from 1001px wide and 640px tall; below
    that the panels stack and the page scrolls.
- Tablet (≤ 1000px): stack the two panels, Paper first.
- Mobile: Stack panels vertically.
- Ensure buttons, inputs, and log entries remain usable at all screen
  sizes.

## 8. Implementation Rules

- Reuse common panel/header components wherever possible.
- Do not implement different styling rules for individual panels.
- Maintain a shared color palette and spacing system.
- Do not change application functionality or backend behavior while
  applying UI styling.
- Preserve existing API integrations, MCP interactions, polling
  mechanisms, and application state.
- Avoid unnecessary component rewrites.

## 9. Acceptance Criteria

The implementation is complete when:

1. Every major functional section is in a rounded panel, as a panel of
   its own or as a tab of the Paper or Activity panel.
2. Each panel uses its own soft section colour (tint and matching border)
   from the table in section 4; content inside sits on white.
3. Every heading is bold and positioned at the top-left.
4. Panel actions, when present, are positioned at the top-right.
5. Colors are consistent with the defined palette.
6. Panel spacing, padding, and border radius are consistent.
7. The dashboard looks clean and professional.
8. Existing application functionality remains unchanged.
9. The layout works on desktop, tablet, and mobile.
10. At 1440×900 the page does not scroll
    (`document.documentElement.scrollHeight <= innerHeight`), before,
    during and after a conversion, with every tab and the Run check
    drawer in use.

**Primary objective:** Consistency across the entire application. Every
panel should look like part of the same product, not an independently
designed component; the soft section colour is the only thing that tells
panels apart.
