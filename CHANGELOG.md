# Changelog

## v1.10.0 — 2026-11-07

Rides can now come straight from FIT and GPX files, through a watched folder or an optional upload page, with no cloud account. Nothing changes for an install that turns neither on, and no metric is recalculated.

### New Features

- **Import FIT and GPX files from a folder.** Set `VELOMATE_IMPORT_DIR` to a host folder and the ingestor reads it at startup and on every poll. It takes `.fit` and `.gpx`, gzipped or not and inside `.zip` archives, so a Garmin or Strava bulk export can go in whole. The folder is mounted read-only and files are never moved or deleted. Each ride file gets one outcome, logged as `[file] <name>: <outcome>` and kept in a new `imported_files` table, and the same bytes are never parsed twice. One bad file doesn't stop the files after it, and a big first load spreads over several polls. Asked for in issue 2 by lparolari, DonkeyShine and TheMBeat, whose fork imported FIT files first.
- **Upload page.** Set `COMPOSE_PROFILES=upload` to start a small page on port 3022 where you drag in the same files and see one line per ride, with a link to its Activity Details. Sign in with a Grafana login that is Editor or Admin. It speaks plain HTTP, so put a TLS reverse proxy in front of it for anything beyond a trusted LAN. Without the profile there is no extra container or port.
- **A file never duplicates a ride you already have.** Files rank below intervals.icu, Strava and Ride with GPS, so a file matching a stored ride only fills its gaps: an empty column, or a whole stream the ride lacks, such as head-unit power on a phone recording. If the cloud copy arrives after the file, the cloud copy becomes the base.
- **VeloMate runs with no cloud source.** Startup no longer stops when no intervals.icu, Strava or Ride with GPS credentials are set.

### Changed

- **A delete from a cloud source keeps a ride that a file also holds.** Ride with GPS and intervals.icu deletes, and Strava's `reclassify`, now unlink that source instead of deleting the ride.
- **Fitness recalculation and every ride write take a database lock**, so the ingestor and the upload page never write at once. A single ingestor never waits on it.
- **Ingest health counts the import folder when it's set**, so a stuck folder raises the Ingestion Stalled alert like a stuck cloud source. A rest week still doesn't.
- The ingestor image adds the `fitdecode` package.

### Upgrading

```bash
git pull && docker compose up -d --build
```

- No `METRICS_VERSION` bump: your stored numbers don't change. The new tables and the `file_sha256` column add themselves on the first start.
- Both ways in are off until you turn them on. For the folder, set `VELOMATE_IMPORT_DIR` to an absolute path or one starting with `./`. Any other value, such as a bare `rides`, is read by Compose as a volume name and the stack won't start. The folder must be readable by the container's unprivileged user. For the upload page, set `COMPOSE_PROFILES=upload`, and `UPLOAD_PORT` to change the port. See the README's "Importing files" and "Upload page" sections.

3,890 tests, including the file import's real-PostgreSQL tests and a check of the upload sign-in against a real Grafana 12.4.

## v1.9.0 — 2026-11-06

Climb segments and a road safety check for planned routes, on top of a long run of correctness fixes. Power bests, Form, intervals, climbs, power zones and VO2max all change, some of them visibly, and a stalled pipeline can no longer look healthy.

### New Features

- **Climb segments.** A climb detected on two or more rides becomes a fixed segment, and every ride that passes both ends is timed over the same two points. A new Climbs dashboard has a leaderboard, the record effort on a map, and time and pace over the months. Activity Details ranks a ride's efforts against your others. Contributed by lparolari.
- **Road safety check for planned routes.** The router is asked what every route rides on. A route on a motorway, trunk road, their links or an IC/IP route is sent around that stretch, and a plan with no way around fails with `no_safe_route` and names the road. Primary roads and limits of 90 km/h or more are listed in the plan, not excluded. If the check can't run, a plan fails rather than giving an unchecked route.
- **intervals.icu backfill.** `VELOMATE_BACKFILL_MONTHS` now applies to the primary source, so a new install gets its history instead of two weeks. A pass is resumable, one failing ride doesn't stop the rest, and an unfinished one continues on the next start or daily sweep.
- **Rides missed by an outage come back on their own.** The daily sweep used to report rides present on intervals.icu but missing locally. It now ingests up to 25 per sweep.
- **Ingest health.** Overview gains Last Sync and Skipped Rides, and a new Ingestion Stalled alert fires after 6 hours without a successful poll. A poll only counts if every enabled source really delivered, so a failed ride, a window of nothing but Strava stubs or a failed recalculation all hold it back, while a rest week does not.
- **Load Ratio and Weekly Work Below / Above FTP** on Overview. Work is split at each ride's stored FTP, in kJ, and the chart always shows the last 12 weeks.

### Fixes

- **FTP and CP estimates missed the day's own rides.** Each ride's timestamp was compared with midnight, so a ride today didn't count until tomorrow, and a backfilled CP estimate left out the rides on its own date. Both now count the whole day. A ride's own FTP still comes from the rides before it.
- **A ride re-analysed on intervals.icu kept its old numbers.** Its streams were replaced, but NP, TSS, intervals and climbs went on describing the old ones, because recalculation only fills gaps. They are now reset with the streams and rebuilt.
- **Map panels use OpenStreetMap by default.** Grafana's built-in basemap is Carto, whose tiles now read "API KEY REQUIRED" without a key. Set `VELOMATE_MAP_BASELAYER` to use another one. Reported, with a first fix, by lparolari.
- **Power bests counted partial windows.** A 5-minute ride reported a 60-minute best, because a ride's opening seconds counted as an N-minute average. Power Duration Curve and Best Efforts now use full windows only, so short rides show fewer durations, and long rides can show a lower 60-minute best.
- **The Form stat told riders to rest during normal training.** It coloured TSB below -10 red. The stat, the chart, the alert and the route planner now share one set of bands on Friel's scale: high risk below -30, productive -30 to -10, transitional -10 to +5, fresh +5 to +25, detraining above +25. The overtraining alert moves from -25 to -30.
- **Interval classes had holes.** Each class needed a duration band and an intensity band, so an 8-minute effort at 106% FTP or a 20-minute test was discarded. Classes are now contiguous %FTP bands, detected from 75% FTP. Efforts under a minute are a new `surge` class, the stop-and-go accelerations of urban riding, and tempo and sweetspot now appear.
- **Ride with GPS phone recordings had no speed.** They carry position only, so Max Speed, the speed chart and the by-kilometre zones stayed empty. A ride with no speed now gets it from GPS, measured over 5 seconds so noise doesn't set Max Speed, and stored rides pick it up on the next start. Based on a fix by lparolari.
- **Climb detection absorbed flat approaches.** A segment counted as uphill if it rose at all, which diluted a climb's grade and could lose it altogether. Each segment must now rise at 1% on its own, and distance comes from GPS, so a stop no longer adds phantom distance.
- **Power zones used today's FTP for every ride.** Zone panels and the monthly chart now use each ride's stored FTP, the same one IF and intervals use. Time in zone can shift a lot if your FTP has changed since a ride.
- **VO2max read low.** It applied a sub-maximal equation to CP. It now uses Sitko's equation on the best 5-minute power of the last 90 days and that ride's weight.
- **Δ Avg cards invented changes.** With an empty period they showed the whole average as the change. They now show no value.
- **Time W'bal < 25% used the latest W'**, not the one the ride was modelled with. Each ride now stores its own. The W'bal tooltips also stop naming a model the code doesn't implement. The numbers don't change.
- **A cross-source merge deleted and re-inserted the ride**, which changed its id and lost Strava segment climbs and values only the losing copy had. It now updates the row in place.
- **One failed stream write could leave an intervals.icu ride without streams for good.** The row and its streams are now written in one transaction, and the next poll retries.
- **Runs and walks from intervals.icu were stored as rides** and counted in CTL. The sync now keeps cycling types only, and a stored ride retyped upstream is left alone.
- **A non-integer `VELOMATE_FTP` split the thresholds.** Startup and recalculation disagreed on the value. A decimal is now rounded, and a value that can't be read is logged and the last good one kept.
- **A failed recalculation at startup no longer crashes the container**, and it's retried on the next poll instead of waiting for the next ride.
- **15 stat panels emitted a NULL row** on an empty database or a GPS-only ride. They now show no value.
- **Activity Details showed an error on every panel before the first ride.** With no rides the Activity picker is empty, which made all 43 queries invalid SQL. They now show no value until a ride arrives.
- **Route planner.** `--surface mtb` always failed, and avoid zones never reached the router. Routes now stay out of avoid zones, and corridor padding no longer sits on the route line. A bare `--destination` routes direct, and wind is read at `--time` when given.
- **The Ride Types donut on Overview was a dot**, squeezed by the ingest health stats beside it. It has its own height now, with the two stats stacked next to it.
- **The route planner names itself to Overpass.** overpass-api.de can answer Python's default User-Agent with a 406, and the planner swallowed the error, so a plan lost its POIs, surface checks and scenic and safety scoring without saying so. Its calls also gave up after 15 to 20 seconds when answers were taking over 30, so they now wait 60. A plan can take a few minutes when Overpass is slow.

### Changed

- **Grafana's dashboards and provisioning are built into its image**, instead of bind-mounted. A deploy that replaced the checkout could leave Grafana on deleted directories, and a change didn't reach it until it was recreated. After editing either, run `docker compose up -d --build`. An invalid `VELOMATE_UNITS` now fails the build instead of leaving Grafana with no dashboards.
- **Athlete Type is removed.** Its cut-offs had no published source, and every rider mapped to Sprinter.
- **There is no `sprint` interval class.** The detector's 30-second minimum sits where sprints end, so it could never appear.
- **The route planner no longer shortens a ride for productive form.** It used to cut 20% and pick shorter routes below TSB -10, and now does so only below -30.
- **A plan now fails if a start or destination is inside an avoid zone**, or if the zones within reach exceed the public Valhalla server's 10 km perimeter limit.

### Upgrading

```bash
git pull && docker compose up -d --build
```

- **`METRICS_VERSION` 15 → 17** recalculates on the first start, and new columns add themselves. Expect the changes above in your numbers.
- An install that already holds rides doesn't backfill on upgrade. Raise `VELOMATE_BACKFILL_MONTHS` to pull history. A backfilled ride older than a route's first ride can renumber that route, so saved `var-route` links may need updating.
- A backfill doesn't change rides you already hold. To recompute their FTP, TSS, IF and fitness history against the new rides, set `VELOMATE_RESET_RIDE_FTP=1` for one restart, then remove it. That doesn't rebuild detected intervals or stored CP estimates.
- Runs and walks an earlier version stored aren't removed, and they keep counting in CTL. They're stored as outdoor rides, so find them by name and delete them with `DELETE FROM activities WHERE id = <id>;`.
- Climb segments build themselves on the first start from the rides you hold. A climb needs two rides to become one.
- Alerts still need a contact point and a notification policy to reach you, see the README.

3,710 tests. CI now runs on the ingestor image's Python, 3.11, and every dashboard query runs in both unit sets against an empty database, a GPS-only ride and a normal ride.

## v1.8.0 — 2026-09-01

Rides are grouped onto the routes they were ridden on, with a dashboard for comparing attempts.

### New Features

- **Route Comparison dashboard.** Rides on the same route are grouped automatically and compared across attempts — effort trend (NP, avg power, avg HR), efficiency trend (EF and aerobic decoupling), the latest attempt against the route's best and median, and an attempts table drilling through to Activity Details.
- **Automatic route detection.** Each ride's GPS is reduced to a set of ~111 m grid cells, and rides whose cells overlap by 70% or more are the same route. Comparing sets rather than sequences makes this independent of direction, speed, pauses, and where on a loop the ride started. Computed by the ingestor from streams already stored, so a ride groups identically whichever source delivered it.

Progress on a route is measured by effort rather than elapsed time. On a stop-and-go route elapsed time mostly reflects traffic, so a faster day is usually a luckier one.

### Notes

- **`METRICS_VERSION` 14 → 15**, which derives every ride's route signature and clusters the library on the next ingestor start.
- Rides with fewer than 60 usable GPS samples, and indoor or virtual rides, have no route. This is expected rather than a failure.
- The matching threshold is measured rather than tuned: within a route real rides overlap 0.87–0.99, against any other ride at most 0.15, and clustering is identical anywhere between 0.5 and 0.8.
- Route ids are rebuilt on each recalculation, so a bookmarked `var-route=<id>` URL may point elsewhere afterwards. The dashboard itself is unaffected, since its selector is a live query.

## v1.7.0 — 2026-09-01

Zwift rides reach VeloMate without Strava, and aerobic decoupling stops reporting a number for rides too short to have one.

### New Features

- **Zwift connects to intervals.icu directly.** Zwift only ever relayed to Strava, and a Strava-relayed activity is a stub intervals.icu may not re-serve through its API — so a Zwift ride could not reach VeloMate at all once Strava's API moved to a paid tier. Zwift and intervals.icu have since built a direct integration on Zwift's Training API: **intervals.icu → Settings → Zwift → Connect**. Completed rides download automatically, "Download Old Data" backfills up to a year, and planned workouts sync the other way. This resolves the v1.6.0 known limitation for Zwift specifically, and needs nothing installed locally.
  - **Relaying through Garmin or Wahoo does not work**, and is documented as a dead end because it looks like it should. Both deliberately refuse to forward third-party-sourced activities to other platforms, so a Zwift ride landing in Garmin Connect or the Wahoo app never reaches intervals.icu.

### Fixes

- **Aerobic decoupling reported a value for rides far too short to have one.** The only guard was four samples — four seconds at 1 Hz. Decoupling compares the first and second half of a ride, and heart rate lags effort by minutes, so on a short ride the first half is mostly HR still climbing toward its steady value. That reads as enormous drift that never happened: a four-minute ride scored 46.6%, where anything above ~10% is already a strong signal on a real endurance ride. A ride now needs 20 minutes of pedalling, leaving each half a ten-minute average. Measured across the library before choosing the threshold, this blanks exactly one ride — the four-minute one; the next shortest carrying a value is 30 minutes.

### Notes

- **`METRICS_VERSION` 13 → 14**, so the stale decoupling values are recomputed on the next ingestor start.
- **`VELOMATE_TOKEN_FALLBACK` is now settable from `.env`.** The ingestor already read it, but `docker-compose.yml` never passed it through, so the path could not be relocated without editing the compose file.
- A test-suite defect was fixed alongside: one test module replaced `requests` in `sys.modules` with a mock, and because pytest imports every module into one process, that leaked into unrelated modules where `requests.RequestException` was no longer a real exception class.

## v1.6.0 — 2026-08-27

intervals.icu replaces Strava as the primary activity source, four new provider-independent metrics, and a round of correctness fixes from a full technical audit.

### New Features

- **intervals.icu as the primary activity source** — free, and relays Garmin Connect, Wahoo, Zwift, Polar, Suunto and Coros without needing a Strava account. Rolling-window sync with no cursor, so a transient per-ride failure simply retries on the next poll instead of blocking newer rides; a daily reconciliation sweep mirrors deletions. Cross-source dedup gives intervals.icu explicit precedence over Strava and RWGPS, which remain equal to each other. Set `INTERVALS_ICU_ATHLETE_ID` and `INTERVALS_ICU_API_KEY` from intervals.icu → Settings → Developer Settings.
  - **Known limitation:** activities that reached intervals.icu *via Strava* cannot be served back through their API — Strava's terms forbid re-serving Strava data — so they are skipped and reported per batch. Upload directly to intervals.icu, or sync your head unit to it, for full coverage.
- **Coasting time** — seconds spent freewheeling per ride, with the share shown on Activity Details. Context for stop-and-go riding, where a quarter of the ride is commonly at zero power.
- **Work above FTP** — anaerobic work above threshold in kJ, the "matches burned" on a ride, isolated from overall volume.
- **Polarization index** — Treff index over the three-zone model, answering whether training is genuinely polarised or accidentally threshold-heavy.
- **CTL ramp rate** — rolling 7-day change in CTL on Overview, with the 5–7/week caution band. Complements 6w Fitness Δ by reading the *current* rate rather than a six-week comparison.

Coasting time, Work above FTP and Polarization index are per-ride metrics computed by the ingestor from streams VeloMate already stores, so they are identical whichever source delivered the ride and are populated for Strava, RWGPS and intervals.icu rides alike. CTL ramp rate is different in kind — a daily value derived from the CTL series rather than from any ride's stream — but is provider-independent for the same underlying reason: CTL comes from TSS the ingestor computes itself.

### Fixes

- **Aerobic decoupling was unusable on stop-and-go rides.** Zero-power coasting samples were averaged into each half's efficiency factor, making the metric a function of how much the rider freewheeled rather than of cardiac drift. On VI 1.4+ rides that produced readings up to 97% — physiologically meaningless — and sometimes inverted the sign. Coasting is now excluded: mean error against an independent implementation fell from 27.9 to 12.4, and the worst reading from 97% to 9.6%.
- **W'bal used one CP for every ride.** Each ride is now modelled against the CP/W' that was current on its own date. Early-season rides had been modelled against a CP 42–64 W too high — the difference between reading an effort as recovery or as above threshold.
- **A single failing activity could wedge Strava ingestion permanently.** Activities are processed oldest-first with the cursor checkpointed after each, so one deterministically-failing ride aborted the pass and was re-hit every poll, silently stopping all newer rides. Failures are now classified: a malformed payload is skipped, a transient error stops the pass without advancing the cursor.
- **The Strava refresh-token fallback could never run.** `/app/data` was root-owned while the container runs as a non-root user, so the write always failed — silently, behind a bare `except`. The directory is now owned correctly, the token is written atomically at `0600`, and a failure is reported rather than swallowed.
- **Grafana panel arithmetic** — the coasting-share panel divided integers before scaling and read 0% for every ride.

### Notes

- **Strava has been removed as an active source in this deployment.** Its API moved to a paid tier and returns 403; historical Strava data is untouched and rides keep their `strava_id`. The code path remains and re-enables by setting the credentials again.
- **`METRICS_VERSION` 11 → 13.** Two bumps: decoupling excluding coasting, then the new ride metrics and contemporaneous-CP W'bal. Both recompute stored values on the next ingestor start.
- **Audit finding on stream sampling was withdrawn after measurement.** The concern was that NP/CP/W'bal count samples as seconds while streams contain gaps. Real streams are 99.5–99.8% exactly 1 second apart, and the gaps are auto-pause stops the device correctly excludes — filling them would compute NP over elapsed time and moved it *away* from an independent implementation. A `sampling_cadence_s` guard now warns if a ride is ever genuinely sparse-sampled, instead of resampling stored data.

## v1.5.1 — 2026-06-16

### Fixes

- GPS-only rides (recorded without a heart-rate or power sensor) no longer blank the entire **Activity Details** dashboard. A sensor-less ride leaves NP / IF / EF / Work / TRIMP / decoupling NULL; two stat panels (Work, Aerobic Decoupling) emitted a row with a NULL value, and in Grafana 12.4 a `colorMode: value` panel rendering NULL crashes the scene renderer — cascading to blank *every* sibling panel, including the distance/elevation panels that had valid data. Both queries now return no rows (graceful "N/A") when the metric is absent. A regression test asserts every `colorMode: value` per-ride stat panel guards NULLs, so the class can't recur.

## v1.5.0 — 2026-06-15

Ride with GPS as a co-equal activity source, athlete-type and VO2max analytics, and a fully configurable deployment.

### New Features

- **Ride with GPS activity source** — ingest rides from Ride with GPS as an alternative to, or alongside, Strava. Standalone backfill, polling, per-second streams, and the full metric pipeline run on Ride with GPS alone (no Strava account needed). With both sources configured, the same ride arriving from each service is deduplicated automatically. Driven by the `/sync.json` change feed with a server-issued cursor; deletions are mirrored (Ride-with-GPS-only rides removed, dual-source rides unlinked). Static api-key + auth-token auth — no OAuth.
- **Athlete Type classification** — sprinter / pursuiter / rouleur / time-triallist / climber, derived from your W'/CP ratio. Stat card on All Time Progression.
- **VO2max estimate** — computed from Critical Power and weight via the Storer formula, with a VO2max Progression trend on All Time Progression.
- **Form-zone annotation** — TSB threshold colouring on the CTL/ATL/TSB chart (overreached / fatigued / neutral / optimal / fresh / detraining).

### Dashboard Changes

- All Time Progression restructured (10 sections → 7); FTP Progression now reads the Critical Power estimate history.
- Overview fitness section reorganised with the CTL chart as the hero panel, plus layout and colour-consistency passes.
- All 20 All Time Progression panel tooltips added/fixed.
- Critical Power estimates backfilled for historical ride dates.

### Fixes

- Overview period-summary averages no longer go blank when the previous period has no rides. The four average "vs previous period" delta cards (Avg Power / Speed / HR / Decoupling) returned SQL `NULL` for an empty prior window, and a `NULL` value crashes the Grafana 12.4 stat-panel render — which blanked every sibling panel on the dashboard. The delta queries now `COALESCE` to a number.
- Ride with GPS trip metrics (power, HR, cadence, duration, calories) are read from the trip-detail response's nested `metrics` object — they were being read top-level, so Ride with GPS rides would store null power/HR and zero duration (affects Ride with GPS rides only).
- Trainer detection on Ride with GPS rides now reads the correct `is_stationary` field — indoor/trainer rides were being classified as outdoor.
- Overview colour consistency across the fitness panels.
- Athlete Type panel rendering (numeric value + value mappings).

### Configuration & Deployment

- Every `docker-compose.yml` value is now overridable from `.env` via `${VAR:-default}` (defaults preserve previous behaviour). New knobs: `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PORT`, `GRAFANA_PORT`, `POLL_INTERVAL_MINUTES`, plus pinnable image tags `POSTGRES_VERSION` (default `15`) / `GRAFANA_VERSION` (default `12.4.0`). The two passwords (`POSTGRES_PASSWORD`, `GRAFANA_PASSWORD`) are intentionally left without defaults — they must be set.
- Grafana's provisioned datasource now honours `POSTGRES_USER` / `POSTGRES_DB` overrides.
- `GF_SERVER_ROOT_URL` auto-derives from `GRAFANA_PORT` (set `GRAFANA_ROOT_URL` explicitly only when behind a reverse proxy).
- `.env.example` reorganised into sections and made complete.

### Migration from v1.4.0

1. **Pull and rebuild:**
   ```bash
   git pull && docker compose build && docker compose up -d
   ```

2. **At least one activity source is now required** (Strava and/or Ride with GPS). Existing Strava-only deployments are unaffected — no action needed. To add Ride with GPS, set in `.env`:
   ```bash
   RWGPS_API_KEY=...      # ridewithgps.com → account settings → Developers
   RWGPS_AUTH_TOKEN=...   # generated on the same page
   ```
   Adding Ride with GPS to a running Strava deployment backfills only the new source.

3. **New optional env vars** — all have safe defaults: `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PORT`, `GRAFANA_PORT`, `POLL_INTERVAL_MINUTES`. See `.env.example`.

4. **Automatic on first restart:** the `rwgps_id` column and its index are created automatically; Athlete Type, VO2max, and form-zone annotations compute from existing data.

5. **No METRICS_VERSION bump** — existing TSS / NP / IF / CTL / ATL / TSB are unchanged.

6. **Breaking changes:** None for existing Strava users.

### Stats

- 585 tests (up from 483)
- 30 commits since v1.4.0

## v1.4.0 — 2026-04-13

Performance modeling, climb detection, and quality-of-life improvements.

### New Features

- **Critical Power / W' modeling** — Monod-Scherrer 2-parameter fit from mean maximal power at 5 standard durations (1-20 min). Quality gate (R² >= 0.9, >= 4 durations) with graceful fallback to rolling 20-min x 0.95. Replaces the old FTP estimate as the algorithmic diagnostic. CP/W' Progression chart on All Time Progression
- **W'bal time series** — per-second anaerobic battery gauge on Activity Details. Skiba differential model with GoldenCheetah tau. Shows when you drained, how close to empty, where you recovered. Min W'bal and Time below 25% stat cards
- **Durability Profile** — 1st half vs 2nd half power comparison on Activity Details. Ratio-based bars showing how much power you retained at each duration. Durability Index stat card (threshold-coloured green/yellow/red)
- **Training Monotony & Strain** — Foster overreaching warning on Overview. Monotony = mean/stdev of daily TSS over 7 days, Strain = weekly TSS x Monotony. Includes rest days via generate_series
- **Climb detection** — RDP (Ramer-Douglas-Peucker) algorithm for elevation profile simplification + Strava segment enrichment. Named Strava segments where available, RDP detection fills gaps. Strava scoring for categorisation (length x gradient). Detected Climbs table on Activity Details
- **Strava OAuth** — `velomate auth` CLI command. Manual paste flow, works from any machine without port forwarding

### Fixes

- numpy added to container requirements (was missing, broke CP estimation)
- Power-Duration Curve panel type fixed (xychart → barchart)
- W'bal panel type fixed (timeseries → trend, matching existing telemetry)
- Overview Monotony/Strain layout (alongside CTL chart)
- All Time Progression layout cleanup (no gaps, full-width utilisation)
- METRICS_VERSION reset now preserves Strava segment data
- Climb detection: multiple iterations to handle rolling terrain (dynamic thresholds, boundary trimming, RDP rewrite)

### Migration from v1.3.0

1. **Pull and rebuild:**
   ```bash
   git pull && docker compose build && docker compose up -d
   ```

2. **New env var (optional):**
   ```bash
   VELOMATE_WEIGHT=75   # enables W/kg (if not set in v1.3.0)
   ```

3. **Automatic on first restart:**
   - CP/W' estimate computed from existing power stream data
   - W'bal computed for all rides with power data
   - Climbs detected from elevation profiles + Strava segments backfilled
   - New tables (`cp_estimates`, `ride_climbs`) and columns (`w_bal`) created automatically
   - No METRICS_VERSION bump — existing TSS/NP/IF unchanged

4. **Strava OAuth (new users):**
   ```bash
   python3 -m velomate.cli auth
   ```
   Replaces the manual curl flow for getting a refresh token.

5. **Breaking changes:** None.

### Stats

- 483 tests (up from 443)
- 48 commits since v1.3.0

## v1.3.0 — 2026-04-10

Major release: ride analytics depth, dashboard overhaul, new metrics.

### New Features

- **Aerobic decoupling** — stored per ride, trended on All Time Progression. Measures cardiac drift (first-half vs second-half EF) to track aerobic fitness
- **Auto interval detection** — Coggan-style classification (sprint / anaerobic / vo2 / threshold / sweetspot / tempo) from power streams, stored in `ride_intervals` table, displayed on Activity Details + monthly distribution on All Time Progression
- **VI-aware TSS** — rides with Variability Index > 1.30 (urban stop-and-go) now use avg_power instead of NP for TSS/IF calculation, preventing overestimation on high-variability rides
- **HR TSS uses LTHR** — the HR-only TSS fallback path now derives LTHR (~89% of max HR per Friel) instead of using max HR directly, fixing a ~21% underestimation on rides without power
- **Estimated FTP preserved as diagnostic** — `sync_state.estimated_ftp` always holds the algorithmic estimate, even when `VELOMATE_FTP` is configured. Overview shows Configured + Estimated FTP side-by-side
- **Calories** — total and delta on Overview, filling the Period Summary grid to 10 stats
- **W/kg (NP-based)** — per-ride NP/weight on Activity Details + NP/kg Trend on All Time Progression. Uses per-ride `ride_weight` column so historical values are preserved if weight changes
- **`VELOMATE_WEIGHT` env var** — rider weight in kg, stored per ride like `ride_ftp`. Enables W/kg panels. Weight changes preserve historical rides
- **`VELOMATE_BACKFILL_MONTHS` env var** — configurable backfill window. Default `12` months, `0` for full Strava history
- **Auto-backfill on window extension** — increasing `VELOMATE_BACKFILL_MONTHS` triggers re-backfill on next restart

### Dashboard Overhaul

- **Overview redesigned** — single comprehensive dashboard with progressive disclosure. Period Summary uses compact 2×5 grid at full width. vs Previous Period, Trends (6 charts), and Ride Patterns sections expanded by default. Default time range changed to 7 days. Outdoor Records and Ride Map removed (already on All Time Progression)
- **All Time Progression** — added Aerobic Decoupling Trend, NP/kg Trend, Monthly Interval Distribution. Rebuilt layout with no gaps or overlaps, full-width utilisation
- **Activity Details** — added Power Distribution histogram (25W buckets, 7-zone coloured), Detected Intervals table, W/kg panel. Advanced metrics row expanded to 8 (added aerobic decoupling, W/kg)
- **Tooltip consistency** — all colour-coded panels now have matching emoji icons in tooltips
- **Panel count**: 43 + 41 + 44 = 128 panels (up from 98)

### Fixes

- Δ Avg cards NULL-safe CTE pattern replacing COALESCE→0
- Power Distribution Z7 missing `unit: min` override causing separate Y-axis
- Power Distribution buckets widened 10W→25W for cleaner histograms
- Δ Avg HR missing `unit: bpm`
- Outdoor Records + Ride Map gained `sport_type` filtering
- Rolling Weekly Volume description notes it's always all-sport

### Migration from v1.2.0

1. **Pull and rebuild:**
   ```bash
   git pull && docker compose build && docker compose up -d
   ```

2. **New env vars (optional):**
   ```bash
   # Add to .env if desired:
   VELOMATE_WEIGHT=75          # your weight in kg — enables W/kg panels
   VELOMATE_BACKFILL_MONTHS=12 # default, increase for more history
   ```

3. **Automatic on first restart:**
   - `METRICS_VERSION` 9→10 triggers full recalculation of TSS/IF (VI-aware routing) and TRIMP (LTHR fix). This runs once and takes a few minutes depending on ride count.
   - New DB columns (`aerobic_decoupling`, `ride_weight`, `ride_intervals` table) are created automatically via `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`.
   - NP, EF, aerobic decoupling, and intervals are computed for all rides with power stream data.
   - If `VELOMATE_WEIGHT` is set, all rides are stamped with `ride_weight` for W/kg.

4. **Dashboard changes load automatically** — Grafana provisioning picks up the updated JSON files. No manual import needed. Clear browser cache if panels look stale.

5. **Breaking changes:** None. All changes are additive. Existing data is preserved; derived metrics are recalculated with corrected formulas.

### Stats

- 443 tests (up from 370)

## v1.2.0 — 2026-03-27

Point-to-point route planning.

### New Features

- **`--destination` flag** — plan point-to-point routes to a named place or coordinates (`--destination Cascais` or `--destination "38.69,-9.42"`)
- **Unified location parsing** — `--start`, `--waypoints`, and `--destination` all accept both place names and `lat,lng` coordinates
- **Corridor waypoints** — when `--destination` + `--distance` is set and the direct route is shorter than target, smart waypoints are added in a corridor to pad the distance
- **There-and-back routing** — `--destination Cascais --loop` routes to the destination and back home
- **Coordinate bounds validation** — `parse_location` rejects out-of-range lat/lng values before they hit Valhalla

### Breaking Changes

- **Waypoints separator** changed from comma to semicolon (`--waypoints "Cascais;Estoril"`) to avoid ambiguity with coordinate notation

### Changes

- **`--duration`/`--distance` now optional** when `--destination` is set
- **`--loop` auto-disables** when `--destination` is set (override with explicit `--loop`)
- **Log warnings** for flag clashes: baseline exceeds target distance, explicit waypoints skip padding

### Fixes

- CI venv pip bootstrap on macOS runner (stale `/tmp` venv, broken pip RECORD)
- Push-to-github script: auto-generated commit messages, graceful first-push, MESSAGE override

### Usage

```bash
python3 -m velomate.cli plan --destination Cascais
python3 -m velomate.cli plan --destination Cascais --waypoints "Oeiras;Estoril"
python3 -m velomate.cli plan --destination Cascais --loop
python3 -m velomate.cli plan --destination Cascais --distance 50km
```

### Stats

- 370 tests (up from 331)

## v1.1.0 — 2026-03-25

Metric accuracy overhaul, per-ride FTP, user feedback fixes.

### New Features

- **IF, VI, TRIMP** stored per ride (previously only existed as Grafana SQL)
- **NP** computed in Python using Coggan 30s SMA (matches GoldenCheetah)
- **Per-ride FTP** — historical rides preserve their TSS/IF accuracy via `ride_ftp` column + 90-day rolling backfill
- **Z7 Neuromuscular** (>150% FTP) added to all power zone panels
- **`VELOMATE_RESTING_HR`** — configure resting heart rate for TRIMP
- **`VELOMATE_RESET_RIDE_FTP=1`** — one-shot flag to reset all per-ride FTP values

### Fixes

- TRIMP: HRR capped at 1.0 (no more exponential blowup when HR exceeds configured max)
- TSS: uses per-ride FTP, not current global FTP
- Configured FTP stamps all rides directly (no more stream re-estimation)
- Decoupling includes coasting samples
- FTP/HR fallbacks standardised across all Grafana panels
- Config changes trigger automatic recalculation
- Default passwords in `.env.example` (zero-edit `docker compose up`)
- Windows emoji encoding fix in map preview

### Breaking Changes

- **`METRICS_VERSION` 6→7** — first startup recalculates all metrics (automatic, may take a minute)
- **Configured FTP overrides estimation** — `VELOMATE_FTP` now applies to all rides directly. Previously it was only a fallback when stream-based estimation returned no result
- **Resting HR changes reset TRIMP** — previously had no server-side effect

### Upgrade

```bash
git pull && docker compose up -d --build
# Wait for "Calculated N days of fitness data" in logs
```

Optional `.env` additions:
```bash
VELOMATE_RESTING_HR=60
VELOMATE_FTP=175
```

## v1.0.0 — 2026-03-21

Initial release. Strava ingestion, 3 Grafana dashboards (98 panels), CLI route planner with 10 data sources.
