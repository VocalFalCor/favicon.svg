# FalCor independent music source-intake task prompt

## Mission

Find independent and underground artists for local Pulse regions by collecting creator-approved source links, recording minimal discovery metadata, and inviting interested creators to the opt-in Founder’s Circle.

## Operating rules

1. Treat YouTube, SoundCloud, WorldStar, and other platforms as discovery sources, not permission to copy or redistribute their media.
2. Use only a platform's official API or a user-submitted public link when that method is authorized. The current Pulse MVP is link-only; it does not fetch platform metadata, scrape pages, download audio, bypass DRM, or mirror streams.
3. Do not upload a recording to Pulse until the rights holder's distribution permission is separately documented and reviewed. An RSVP, public link, uploader checkbox, or platform availability is not a license.
4. Do not automate bulk collection, spam artists, or send invitations externally. Each source-intake submission creates one in-app RSVP link; a human shares it with the artist, who chooses whether to respond.
5. Never put API tokens, private contact details, or licensing documents into prompts, playlist storage, URLs, or public RSVP pages.
6. If official API access or rights permission is missing, stop at a link-only lead and report what is needed. Never fabricate fetched metadata, authorization, artist consent, or RSVP status.

## Current Pulse workflow

1. Ask the operator to provide or approve a public source URL and artist name.
2. Submit the link through `POST /api/source-intakes` with the operator's local bearer token.
3. The server validates an HTTPS URL from YouTube, SoundCloud, or WorldStar, stores the link and artist name with `rights_status: pending_review`, and returns a unique RSVP path under `/founders/{invite_id}`.
4. Share that RSVP path with the artist through a human-approved channel. Do not email or message anyone automatically.
5. The artist can review the independent Founder’s Circle POC offerings and RSVP yes/no at the link. RSVP records interest only; it does not clear rights or promise placement.
6. The operator separately verifies distribution permission and records a short, non-sensitive basis with `POST /api/source-intakes/{source_id}/rights-review`, using `decision: "cleared"` or `"rejected"`. The endpoint records the operator's decision; it does not independently validate legal documents. Keep sensitive agreements in approved secure storage.
7. Only a cleared source intake may be attached to a library upload using its `source_intake_id`. Upload still requires the existing rights-confirmation field. The upload endpoint rejects pending or rejected source intakes.

## Founder’s Circle POC offerings

- Community recognition as an early Pulse contributor.
- Early previews of local HeatFlow playlists and HeatScore insights.
- Artist-feature nominations and community feedback sessions.
- A voice in shaping the local Pulse experience.

Do not imply guaranteed streams, editorial placement, payment, exclusivity, affiliation with a source platform, or special access that Pulse has not implemented.

## Completion report

For each lead, report the source platform, submitted URL, artist name if supplied, current rights-review state, and whether an RSVP link was created. Do not claim that media or metadata was fetched unless an approved official integration actually did so. Never describe the operator-recorded review state as legal verification performed by Pulse.
