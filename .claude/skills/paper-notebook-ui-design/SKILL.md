---
name: paper-notebook-ui-design
description: UI design guidelines for the Paper-to-Notebook dashboard (api/static). Use whenever adding, changing or restyling a dashboard panel, its HTML/CSS/JS, colours, spacing or layout, so every panel looks like part of the same product.
---

# Paper-to-Notebook UI Design Guidelines

## Where this lives in the code

- `api/static/base.css` defines the palette, spacing and shape as CSS
  tokens (`--bg`, `--card`, `--border`, `--accent`, `--accent-hover`,
  `--text`, `--muted`, `--subtle`, `--ok`, `--warn`, `--error`, `--radius`,
  `--radius-sm`, `--pad`, `--gap`, `--font`, `--mono`), the dashboard grid,
  the shared panel frame (`section.panel`), the panel header (`.panel-head`,
  `.panel-head h2`, `.panel-actions`) and shared controls (`button.link`).
- Each panel is `api/static/panels/<name>/<name>.html|.css|.js`. Panel CSS
  must use the tokens from `base.css`; do not add literal colours, radii or
  per-panel header styles.
- A new panel: add its folder, a `<link>` and an
  `<!-- include: panels/<name>/<name>.html -->` line in `index.html`, a grid
  area in `base.css` (desktop, tablet and mobile), and wire it in `app.js`.
- Layout (desktop): row 1 Configuration | Upload paper / specify link |
  Download; row 2 LLM output (two columns) | MCP activity log; row 3
  Metrics (full width). Tablet: two columns. Mobile: one column.
- Check changes visually at desktop, tablet and phone widths before
  finishing, and run `pytest` (the page tests check the panels render).

## 1. Objective

Establish a consistent, clean, modern SaaS dashboard UI for the
Paper-to-Notebook application.

The application should have a professional appearance, with clearly
separated functional panels, consistent spacing, typography, colors, and
alignment.

## 2. Global Design Principles

- Use a pure white page background (`#FFFFFF`).
- Use white backgrounds for all functional panels.
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

- Background: `#FFFFFF`
- Border: `1px solid #E2E8F0`
- Border radius: `10px` to `12px` (implemented: `--radius: 12px`)
- Internal padding: `12px` to `16px` (implemented: `--pad: 16px`)
- Consistent spacing between panels (implemented: `--gap: 16px`).
- Avoid heavy shadows.
- All panels must have consistent styling.

### Panel heading

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
| Panel background | #FFFFFF | `--card` |
| Panel border | #E2E8F0 | `--border` |
| Primary accent | #2563EB | `--accent` |
| Primary hover | #1D4ED8 | `--accent-hover` |
| Main text | #1E293B | `--text` |
| Secondary text | #64748B | `--muted` |
| Success | #16A34A | `--ok` |
| Warning | #D97706 | `--warn` |
| Error | #DC2626 | `--error` |
| Muted background | #F1F5F9 | `--subtle` |

Use blue as the primary interactive color.

Use green, amber, and red only to communicate status.

Do not introduce additional colors without a functional reason. (The one
exception in use: the Metrics stage bar is a grey-to-blue scale in stage
order, with amber only for "retrying".)

## 5. Application Panel Structure

Apply the common panel design to the following sections.

### Configuration
- Display provider, model, API key status, save folder, and generation
  options.
- Keep labels and values aligned.
- Use clear status indicators.
- Provide Refresh at the top-right when applicable.

### Upload Paper / Specify Link
- Provide a PDF upload/drop area.
- Provide a separate arXiv URL or ID input.
- Make the primary Convert to Notebook button visually prominent.
- Clearly communicate upload and processing states.

### Download
- Display the generated notebook information.
- Provide relevant download/open actions.
- Show an appropriate empty state when no notebook is available.
- Provide Refresh at the top-right.

### LLM Output
- Display generated explanations and notebook-generation output.
- Support readable text formatting.
- Clearly distinguish loading, completed, and error states.

### MCP Activity Log
- Display MCP server activity in chronological order.
- Show server connection status.
- Display tool calls, arguments, execution status, and results where
  appropriate.
- Provide Clear at the top-right.
- Use readable timestamps and subtle status indicators.
- Support automatic polling without disrupting the user's interaction.

### Metrics
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

- Desktop: Use a multi-column dashboard layout.
- Tablet: Reduce the number of columns while preserving panel separation.
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

1. Every major functional section is enclosed in a rounded panel.
2. All panels use a consistent white background and subtle border.
3. Every heading is bold and positioned at the top-left.
4. Panel actions, when present, are positioned at the top-right.
5. Colors are consistent with the defined palette.
6. Panel spacing, padding, and border radius are consistent.
7. The dashboard looks clean and professional.
8. Existing application functionality remains unchanged.
9. The layout works on desktop, tablet, and mobile.

**Primary objective:** Consistency across the entire application. Every
panel should look like part of the same product, not an independently
designed component.
