# Grafana Dashboard Functionality & Value Reviewer Prompt

Use this prompt to evaluate every panel for correctness and value to the user.

---

You are evaluating 3 Grafana dashboards for a self-hosted cycling analytics platform (VeloAI). Your job is to assess every panel for **functional correctness** and **value to a solo cyclist tracking their training**.

Read all 3 dashboard JSON files:
- `grafana/dashboards/overview.json`
- `grafana/dashboards/activity.json`
- `grafana/dashboards/all-time-progression.json`

Also read the database schema (`ingestor/db.py`) and fitness logic (`ingestor/fitness.py`) to understand what data is available.

For each panel on each dashboard, evaluate:

**1. Does this panel work correctly?**
- Does the SQL query reference real columns from the schema?
- Are filters applied consistently (sport_type, time range)?
- Will it produce meaningful results with ~100-200 cycling activities over 12 months?
- Are there edge cases that would produce errors or misleading data? (division by zero, NULL handling, empty results)

**2. Does this panel add value?**
Score each panel 1-5:
- **5 = Essential** — a cyclist would check this every time they open the dashboard
- **4 = Valuable** — provides useful insight, would be missed if removed
- **3 = Nice to have** — interesting but rarely actionable
- **2 = Low value** — the information is available elsewhere or rarely useful
- **1 = Remove candidate** — adds clutter without insight

When scoring, consider:
- Does this panel answer a question the cyclist actually asks? ("Am I getting fitter?", "How hard was that ride?", "Am I overtraining?")
- Is this information already shown on another panel or dashboard?
- Does the panel type suit the data? (e.g., a gauge for a single number vs a timeseries for a single number)
- Would a serious amateur cyclist (rides 4-5x/week, uses a power meter, tracks fitness) find this useful?

**3. What's missing?**
After reviewing all panels, identify gaps:
- Questions a cyclist would ask that no panel answers
- Data available in the schema that no panel uses
- Insights that could be derived from existing data but aren't shown

**Output format:**

For each dashboard, produce a table:
```
| Panel ID | Title | Type | Works? | Value (1-5) | Notes |
```

Then a summary section:
- **Remove candidates** (value 1-2): panels to cut
- **Fix needed**: panels that are broken or misleading
- **Missing insights**: what should be added
- **Redundancy**: panels showing the same thing in multiple places

Be opinionated. A focused dashboard with 15 great panels beats a cluttered one with 40 mediocre panels. If two panels show similar information, recommend keeping the better one.
