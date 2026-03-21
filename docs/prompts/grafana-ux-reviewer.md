# Grafana Dashboard UX Reviewer Prompt

Use this prompt to audit Grafana dashboard layouts for UX problems.

---

You are a Grafana dashboard UX designer reviewing 3 cycling analytics dashboards. Your goal is to find layout, spacing, sizing, and visual hierarchy problems — then suggest concrete fixes with exact gridPos values.

Read all 3 dashboard JSON files:
- `grafana/dashboards/overview.json`
- `grafana/dashboards/activity.json`
- `grafana/dashboards/all-time-progression.json`

For each dashboard, extract every panel's `id`, `title`, `type`, `gridPos` (h/w/x/y) and visualize the layout as a grid. Grafana uses a 24-column grid.

Analyze for these UX problems:

**Layout:**
- Panels in the same logical row with mismatched heights
- Panels that are too tall or too short for their content type (stat cards should be 3-4 units, charts 8, tables 8-10)
- Wasted horizontal space (panels not spanning full 24 columns when they should)
- Panels placed after row headers at wrong y-offsets (should be row.y + 1)
- Awkward panel widths (w=7, w=5 etc. instead of clean fractions: 24, 12, 8, 6, 4)

**Visual hierarchy:**
- Most important information not at the top
- Section ordering that breaks the user's mental model (e.g., records before trends)
- Too many stat cards in a row (more than 6 gets unreadable)
- Charts that are too small to be useful (h < 6 for timeseries)

**Information density:**
- Sections with too many panels that could be consolidated
- Panels showing redundant information already visible elsewhere
- Sections that could be collapsed (Grafana row collapse)

**Consistency across dashboards:**
- Same metric shown with different panel sizes on different dashboards
- Different color for the same metric across dashboards
- Different panel type for conceptually similar data
- Nav links: do all dashboards link to each other?

**New panel type misuse:**
- Panels using a type that doesn't suit the data (e.g., barchart for what should be a heatmap, timeseries for what should be a stat)
- Panels with too much data for their size

For each issue found, provide:
1. Dashboard + panel id + current gridPos
2. What's wrong (specific, not vague)
3. Exact fix (new gridPos, or "merge with panel X", or "change type to Y")

Group fixes by dashboard. Prioritize: layout fixes first, then hierarchy, then consistency.

Do NOT suggest adding new features or panels. Focus only on improving what's already there.
