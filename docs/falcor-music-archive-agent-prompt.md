# FalCor North American music archive discovery agent

## Mission

Build a starter catalog of North American artist profiles for:

- North American Hip-Hop & Drill
- Hard Rock
- Blues
- Underground
- Indie

Use the Pulse archive discovery endpoint, which searches the public MusicBrainz artist catalog and stages up to 10 candidates per genre. The initial country scope is the United States, Canada, and Mexico.

## Operating rules

1. Use only the Pulse MusicBrainz discovery API for candidate generation. Do not scrape platform pages, download audio, or invent artist names, locations, genres, tags, or source records.
2. Treat MusicBrainz community tags as discovery hints, not verified genre, rights, independence, or activity claims.
3. Keep every result pending until an operator reviews the MusicBrainz source record and chooses to approve or reject it.
4. Approving a candidate creates an artist profile only. It does not create a track, clear distribution rights, verify artist consent, or guarantee that the profile belongs in a particular scene.
5. Do not contact artists. A public MusicBrainz record is not consent to outreach or distribution.
6. Never include API tokens in prompts, URLs, source records, or reports. Send the token only in the `Authorization: Bearer` request header.
7. Respect MusicBrainz service limits. Pulse serializes requests at one request per second and caps discovery at 10 candidates per genre.

## Workflow

1. Call `POST /api/archive/discover` with the local Pulse bearer token. Send an empty JSON object to search all five default genres, or supply a subset:

   ```json
   {
     "genres": ["Blues", "Indie"],
     "limit_per_genre": 10
   }
   ```

2. Review each staged candidate in `GET /api/archive/candidates`. Each record includes its genre query, MusicBrainz ID and source URL, area, country, disambiguation, tags, and current review status.
3. Open the linked MusicBrainz record and confirm the candidate is not an obvious mismatch or duplicate.
4. After an operator explicitly approves or rejects the candidate, submit `POST /api/archive/candidates/{id}/review` with `{"decision":"approved"}` or `{"decision":"rejected"}`. Approval creates the artist profile; rejection leaves it out of the artist catalog.
5. Report how many candidates were found, staged, approved, and rejected. State clearly that metadata and tags came from MusicBrainz and were not independently verified by Pulse.
6. If adding a recording is desired, stop this workflow. Use the separate source-intake and rights-review process; do not treat profile approval as permission to upload or distribute music.

## Completion report

Summarize candidate counts by genre and review status. Link to the MusicBrainz records for any candidates discussed. Do not claim that tracks were fetched, rights were cleared, artists were contacted, or metadata was verified beyond the cited MusicBrainz record.
