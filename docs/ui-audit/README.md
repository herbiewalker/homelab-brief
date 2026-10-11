# Interface audit screenshots (Task 38, version 0.0.55)

Screenshots of the operations-console look in a real browser, on the synthetic sample data (`scripts/ui_preview.py`), so nothing here is from a real installation. The first set (Task 37, version 0.0.53) was replaced when the whole site moved to the console design.

| File | Page | Width | Theme |
| --- | --- | --- | --- |
| `overview-1280-dark.jpg`, `overview-1280-light.jpg` | Overview: banner, KPI tiles with 24-hour trend lines, systems with 30-day bars, incident timeline | 1280 | dark, light |
| `overview-360-dark.jpg` | Overview on a phone: KPI tiles two across | 360 | dark |
| `findings-1280-dark.jpg` | Findings and alerts (a list page on the shared panel style) | 1280 | dark |
| `status-1280-dark.jpg` | The public status page `/` (no script, no login) | 1280 | dark |
| `brief-1280-dark.jpg` | The static brief (`/brief.html`), also used on tablets | 1280 | dark |
| `signin-1280-dark.jpg` | Sign in | 1280 | dark |

How the audit was run: `scripts/ui-audit.js` was loaded into every admin page (23 pages in the side menu) at 1280 px in both themes, and into the Overview at 360 px. It reported no horizontal scrolling, no text below WCAG AA contrast, no control without an accessible name, no uncaptioned data table, and no tap target under 24 by 24 pixels (one 26 by 23 pixel button on the Findings page was found and fixed). The public status page cannot load the audit script (its content security policy allows no script), so it was checked for horizontal overflow by measurement at 360 px and by eye. `tests/test_ui_contrast.py` repeats the contrast check on every build from the colour tokens, for the default dark theme and both ways of getting the light one.

The screenshots are a record of the state at this version, not a style guide: a later change to a page is expected to make its image out of date.
