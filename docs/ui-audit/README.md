# Interface audit screenshots (Task 37, version 0.0.53)

Screenshots of key admin pages in a real browser, in both themes, at the three widths the interface is built for: 1280 px (desktop), 768 px (tablet), and 360 px (phone). They use the synthetic sample data, so nothing here is from a real installation.

| File | Page | Width | Theme |
| --- | --- | --- | --- |
| `overview-1280-dark.jpg`, `overview-1280-light.jpg` | Overview | 1280 | dark, light |
| `findings-1280-dark.jpg`, `findings-1280-light.jpg` | Findings and alerts | 1280 | dark, light |
| `priorities-1280-dark.jpg`, `priorities-1280-light.jpg` | Priorities and alerts | 1280 | dark, light |
| `ai-1280-dark.jpg` | AI and agents (Settings tab) | 1280 | dark |
| `overview-768-dark.jpg`, `findings-768-light.jpg` | Overview, Findings | 768 | dark, light |
| `overview-360-dark.jpg`, `ai-360-light.jpg` | Overview, AI and agents | 360 | dark, light |
| `page-not-found-360-dark.jpg` | The page shown for an unknown address | 360 | dark |

How the audit was run: `scripts/ui-audit.js` was loaded into every admin page (29 pages, plus the sign-in, status, and error pages) at 360, 768, and 1280 px, in both themes. The final run reported nothing at all three widths: no horizontal scrolling, no text below WCAG AA contrast, no control without an accessible name, no uncaptioned data table, no tap target under 24 by 24 pixels. `tests/test_ui_contrast.py` repeats the contrast check on every build from the colour tokens.

The screenshots are a record of the state at this version, not a style guide: a later change to a page is expected to make its image out of date.
