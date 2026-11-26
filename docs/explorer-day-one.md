# Explorer outline and day-one delivery timeline

## Purpose

Explorer is the local discovery surface for finding artists and authorized tracks, understanding the local HeatScore signal, previewing music, and curating a HeatFlow queue. It builds on the existing Pulse API and landing page rather than introducing a separate catalog or scoring system.

## Main outline

1. **Explore the local catalog**
   - Show artist name, area, genre, bio, HeatScore, recent listens/votes, and Heat gain check state.
   - Show authorized tracks and keep the current empty-catalog/sample-score distinction clear.
2. **Find a relevant artist**
   - Search artist and track names.
   - Filter by configured area and genre; make the active filters visible and easy to clear.
   - Sort by HeatScore and recent activity, with deterministic tie-breaking.
3. **Understand the signal**
   - Explain that HeatScore is a local activity/freshness measure, not a quality rating or a promise of popularity.
   - Surface score and gain changes from the existing API; do not count page refreshes or time decay as new hits.
   - Label area as artist-provided/configured data, not verified location.
4. **Listen and curate**
   - Preview only audio available through the authorized Pulse library.
   - Add tracks to HeatFlow, select the current queue item, and open the separate player.
   - Keep playback controls stable across LIVEPULSE refreshes.
   - Erase from the playlist only; never imply that this removes uploaded library audio.
5. **Participate locally**
   - Offer a local vote only when the API says the artist matches the configured listener area.
   - Keep voting optional and explain the shared-token, single-profile proof-of-concept boundary.
6. **Handle failure and accessibly**
   - Provide useful loading, empty, offline, invalid-token, and no-results states.
   - Keep controls keyboard-accessible, labeled, and visibly focused; honor reduced-motion preferences.
   - Keep the API token out of URLs and persistent playlist storage.

## Day-one completion criteria

The first usable Explorer release is complete when a listener can search and filter the existing catalog, sort artists by the displayed signal, open an artist's tracks, play authorized audio without LIVEPULSE interrupting it, add/select tracks in HeatFlow, use the separate player's play/pause, seek, shuffle and queue-only erase controls, and understand when the API or token is unavailable. Focused automated tests and a manual browser pass should cover those flows.

The repository already has the live catalog, HeatScore and gain counter, LIVEPULSE refresh, authorized uploads/listens/votes, and the HeatFlow queue/player implementation. Explorer search, filters, and sorting are still planned work; the HeatFlow changes also need a live-server/browser pass after the server is updated.

## Realistic estimate

Estimate: **3 focused working days for one developer**, assuming the existing local API, artist data, upload flow, and HeatScore remain in scope and no account, hosting, or product-design changes are introduced. This is an estimate for a usable local MVP, not a public launch. The API and live catalog already exist; the remaining uncertainty is in browser integration and edge-case validation.

| Day | Work | Exit check |
|---|---|---|
| 1 | Build the Explorer catalog controls: text search, area/genre filters, HeatScore/activity sorting, filter reset, and useful empty/no-results states. Keep the existing live catalog and score display as the source of truth. | Search, each filter, sorting, and reset work against the current artist/track data without changing server state. |
| 2 | Connect and validate the existing HeatFlow implementation: browser-local persistence, same-origin pop-out handshake, artist area/HeatScore/time display, play/pause, ±10-second seek, shuffle, and queue-only erase. Preserve the active audio element through live refreshes. | A track can be found in Explorer, played, queued, and controlled from the pop-out without removing library audio. |
| 3 | Add/complete focused route and behavior tests; test token failure, offline/empty states, popup blocking, storage synchronization, keyboard use, responsive layout, and playback interruptions. Fix issues found and update run/docs instructions. | Targeted tests pass and a manual browser checklist confirms the end-to-end local flow. |

If work must fit into one calendar day, reduce scope to artist/track search, area filter, HeatScore sort, and a stable HeatFlow queue/player using the existing API; defer genre filtering, secondary sorting, and broad browser compatibility checks. Do not call that compressed cut fully validated.

## Explicitly outside day one

- Public accounts, per-user permissions, multi-tenant profiles, or paid tiers.
- Verified geolocation, external music catalogs/streaming, or automated rights verification.
- Recommendations from a model, n8n automation, or an agent that casts votes/publishes without confirmation.
- Multi-worker LIVEPULSE delivery, cloud media storage, and production deployment/monitoring.
- Permanent library deletion from the HeatFlow erase control.

These are separate design and security milestones; they should not be hidden inside the local Explorer MVP estimate.
