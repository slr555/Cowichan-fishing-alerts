# Cowichan-fishing-alerts
Auto alerts for when fishing conditions will be good

# Cowichan fishing alert (v2)

Runs twice a day (about 6am and 5pm Pacific) on GitHub Actions. It reads the Cowichan gauges and the weather forecast, scores five fisheries against Cowichan-specific flow bands and the regulation calendar, and pushes to your phone through ntfy only when something changes.

## What it watches
- **Flow:** Water Survey of Canada 08HA011 (near Duncan) and 08HA002 (weir release at Lake Cowichan).
- **Weather:** Open-Meteo. Rain comes from Lake Cowichan; temperature, barometer, cloud and sunrise/sunset come from Duncan.
- **Regulations:** the DFO Region 1 salmon table and the BC in-season changes page. You get an alert when a Cowichan line changes.
- **Its own health:** a failed or stale gauge feed sends a "data problem" alert (at most once a day), then a "data feeds back" note when it recovers. The regulation watcher alerts after 3 failed reads in a row, which usually means a page was redesigned.

## Fisheries scored
| Fishery | Season | Good flow (m3/s) | Notes |
|---|---|---|---|
| Coho | Oct to Jan (peak Nov) | 15-60 | bonus for fresh-fish push flows of 28-45 |
| Chum | Oct to Dec | 12-80 | retention TBD, treated as C&R |
| Winter steelhead | Dec to Apr (peak Feb-Mar) | 25-90, fishable to 150 | "fish the soft edges" note at high flow |
| Trout egg bite | Oct to Jan | 10-45 | ramps up after the first fall freshet or weir pulse |
| Spring trout | Mar to Jun | 8-40 | salmon-fry streamer bonus from late March |

Every score is gated by the regulation calendar in `open_sections()`, built from the 2025-2027 synopsis and the DFO table. If nothing is open for a species on that date, it scores 0. A drought hold caps scores in Sep-Oct when Duncan is below 7 m3/s.

## When it pushes
- A species moves up to GOOD (priority 4), or drops out of GOOD ("window closing", priority 2).
- A new heads-up appears: a weir release was raised, a window is forming, rain is coming to freshen the river, or good conditions are about to blow out.
- A regulation change or a data problem.
- **Thursday's 5pm run** also sends a weekend outlook. It ranks Fri, Sat and Sun on a rough flow projection and includes the strategy card for the best bet.

Each alert includes a **strategy card** for the GOOD species: the open section and its gear rule, spots, gear, fly, the play, and sunrise/sunset.

## Setup
1. Install the **ntfy** app and subscribe to a random topic, e.g. `cowichan-x7k2q9`. Anyone who knows the topic can read it.
2. Create a **private** GitHub repo containing `cowichan_alert.py` and `.github/workflows/cowichan-alert.yml`.
3. Under Settings → Secrets and variables → Actions:
   - Add the secret `NTFY_TOPIC`.
   - Optionally set the variable `ALWAYS_SEND=true` to get every run while you calibrate.
4. Go to Actions → Cowichan fishing alert → Run workflow. Tick "weekend outlook" to test that too.

The workflow commits `state.json` after each run. That file is the alert's memory: last ratings, heads-ups already sent, regulation baselines and the season's first fall freshet. The first run captures the regulation baselines silently. The regular commits should also keep GitHub from disabling the schedule for inactivity.

## Testing locally
```
python cowichan_alert.py --dry-run            # live data, no push, no state saved
python cowichan_alert.py --demo nov-drop      # synthetic: coho/chum GOOD on the drop
python cowichan_alert.py --demo oct-pulse     # weir release raised in a drought year
python cowichan_alert.py --demo drought       # drought hold, nothing pushed
python cowichan_alert.py --demo feb-steel     # steelhead GOOD after a freshet
python cowichan_alert.py --demo thursday      # Thursday run with the weekend outlook
```
Demos don't save state unless you set `STATE_FILE` yourself.

## Tuning
Everything you'd want to adjust is at the top of the script:
- `SPECIES`: flow bands and seasonal weights.
- `CARDS`: gear and play text.
- `PROJ`: the weekend flow projection.
- `DROUGHT_FLOW`.

The weekend projection is the weakest part. It's a simple recession-plus-rain model and should be backtested before you trust its numbers.

## Limits
- The regulation calendar expires with the synopsis on 2027-03-31. Alerts will warn you after that date.
- Mile 66 to Sandy Pool from Sep 1 to Nov 15 is flagged as unclear: DFO lists it open for salmon catch and release, but the provincial table lists it closed. Confirm with DFO Duncan (250-746-6221).
- This does not replace reading the current notices before you go.
