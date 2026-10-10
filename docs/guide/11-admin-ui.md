# The admin interface: a complete rebuild

Goal: everything is configured and controlled by clicking, in a clean, responsive interface that feels like a professional product. The text editor for each file stays as an "Edit as text" option for people who want it; it is never the only way. This is Phase 4, Tasks 31 to 37.

## Why

The first admin pages (Tasks 15 and later) were built to prove each feature, not to be used every day. In use, this shows:

- A setting that has a placeholder in the shipped file (a server address, a key variable) cannot be set in the GUI at all. The only fix is to open a YAML editor, which is how the Unraid collector setup failed for the first real user.
- Most of the 14 config files have no form. Systems, Network, the AI switches, and the collector on/off switch do; ignore rules, priorities, playbook thresholds, schedules, retention, notifications, devices, topology, map, and views do not.
- Pages are long single columns of tables and unstyled forms (one 91-line stylesheet). Saving reloads the page. Errors are a red box at the top. There is no feedback while something runs, no consistent empty or loading state, and the side menu has 17 flat links.

## Principles

1. **Every setting has a control.** A form field for each value in each config file, with a label, a plain-language hint, units, limits, and the shipped default shown beside it. Lists (ignore rules, collector servers, systems, devices, topology nodes) are row editors with add, edit, delete, and reorder. A control writes the file through the config editor, so the change is validated, versioned (undo is one click), and audited exactly as before.
2. **Edit as text stays.** Every settings page has an "Edit as text" link to the existing editor, with its diff, history, and rollback. The form and the text editor always show the same file; a change in one is visible in the other.
3. **Never a dead end.** A setting that is refused says why next to the field, and the form offers the fix (for example, a placeholder address is an empty field with an example, not a value to find and edit in YAML). A switch is disabled with a reason until its prerequisites are met, rather than failing on click.
4. **Do not lose what the person wrote.** Scalars are changed in place (comments, order, and spacing survive; `yamlscalar.py`). A list is regenerated from its rows with the file's header comment kept; the previous text is always in the history.
5. **Feedback for everything.** Saving, testing, and running update the page in place (no reload), show a progress indicator while they work, and end in a toast or an inline result that says what happened and offers Undo where one exists. Destructive actions ask first in a dialog that names what will happen.
6. **Responsive and accessible.** Usable from 360 px (phone) to a wide desktop: the side menu becomes a drawer, tables become stacked cards or scroll inside their card, forms go to one column. Keyboard-reachable with visible focus, labelled controls, enough contrast in both themes, no information carried by colour alone, and `prefers-reduced-motion` respected.
7. **Calm and consistent.** One set of spacing, type, colour, and component rules (design tokens). Light, dark, and follow-the-system themes. Status always looks the same (green, amber, red, grey with an icon and a word).
8. **Same constraints as before.** No build step, no external requests (fonts, icons, scripts, and styles are served from this container), a Content-Security-Policy with no inline script, and only the vendored htmx, Alpine (CSP build), and Cytoscape. Icons are an inline SVG sprite drawn for this product.

## Structure

The side menu is grouped; each group is one page of cards, not a long list of links:

| Group | Pages |
| --- | --- |
| Monitor | Overview, Findings and alerts, Sources, Network map |
| Configure | Systems, Collectors, Detection (ignore rules, priorities, playbook thresholds), Devices and topology, Notifications, Schedule and retention, Network and access |
| Intelligence | AI (providers, chains, guardrails, prompts), Agents, Proposals, Evaluation, MCP clients |
| System | Backups and exports, Config files (text), Audit log, Log search, Replay and tuning, Account |

A top bar carries the instance status (run ok, silent hosts, open alerts, collectors failing), a theme switch, and the account menu. Every page has one primary action and a clear "what this page is for" line.

## The settings framework (Task 33)

One generic mechanism, so every file gets its form the same way:

- **Field specs.** A small declarative description of each file's settings: path, type (number, switch, choice, text, duration, time, list of rows), label, help text, unit, minimum and maximum, and the shipped default. The same spec drives the form, the validation message, the "reset to default" button, and the documentation table.
- **Writers.** Scalars use `yamlscalar.set_path`. Row lists use a generator per file that keeps the header comment and writes the rows in the file's own style.
- **Save path.** Every form posts to the config editor's save: digest check (two browsers cannot overwrite each other), the file's own validator, a history entry, an audit row, and the configuration guard. The page then shows a toast with Undo (a rollback to the version just replaced).
- **Secrets.** A key or password is never stored in a file or shown. A field for one is a variable name plus a "set / not set" badge and a short how-to for setting the variable on the container.
- **Checks next to the control.** A collector form has Test connection; an ignore rule has "test this pattern against recent lines" and shows how many it would match; a threshold shows the value it would have flagged last week (using replay).

## Overview (Task 32)

The first screen answers "is everything fine, and if not, what do I do?":

- A headline strip (run ok or "no data today", with the reason and the one action that fixes it).
- Health cards: pipeline (events, lag, silent hosts), storage (archive size and days left), memory, collectors, AI and agents, alerts open, proposals waiting.
- System tiles with state, a one-line summary, and a link to that system's findings.
- Top findings with ack and snooze inline, and a "what changed since yesterday" strip.
- Setup checklist for a new install (account, sources repointed, systems named, collectors, notifications), each step done or one click away.
- Live refresh without flicker; empty states that say what to do next.

## Done when

- Every setting in every config file can be viewed and changed from a form, and the form and the text editor agree.
- A fresh install can be fully set up, including a collector with its server address, without opening a text editor.
- No page needs horizontal scrolling at 360 px; the menu works by keyboard and touch; both themes pass a contrast check.
- Saving never reloads the page; every write shows a result and (where possible) an Undo.
- The pages are checked in a real browser at phone, tablet, and desktop widths, in both themes, with screenshots kept in the release notes.
