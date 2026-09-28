# Privacy, consent and retention

## What Recall processes

Recall processes only these meetings:
- Teams meetings where **Teams transcription was turned on**. Participants see Teams' own recording and
  transcription notice, which is the consent mechanism. Recall never joins calls or records audio itself.
- Files a signed-in user explicitly uploads.

Transcripts that don't map to a configured team are skipped and not stored.

## Where data goes

| Data | Where it lives |
|---|---|
| Transcripts, recaps, action items, embeddings, audit log | Your PostgreSQL database, in your Azure region |
| Uploaded recordings | Blob storage in the same region, deleted after the media retention period |
| Summarization and question answering | Transcript text and questions go to the **Anthropic API** (Claude) |
| Embeddings | Computed locally in the container by default. With `EMBEDDINGS_PROVIDER=voyage`, text goes to Voyage AI |
| Speech-to-text (uploads only) | Local whisper by default. With a Deepgram key, audio goes to Deepgram |

If your organization requires processing to stay in Azure, set `ANTHROPIC_PROVIDER=foundry` with
`FOUNDRY_RESOURCE` and `FOUNDRY_API_KEY`. Claude calls then go through Microsoft Foundry
(`anthropic.AsyncAnthropicFoundry`); nothing else changes. Keep the default local embeddings and local
speech-to-text as well.

## Access

- A person sees a meeting only if they attended it or belong to its team. Admins see everything.
- Team-scoped answers use only that team's meetings. This is also why the answer cache is shared per team.
- Every question is written to `audit_log` with the asker, the team and the routing decision.

## Retention (per team)

| Data | Default | Column |
|---|---|---|
| Uploaded media | 30 days | `teams.retention_media_days` |
| Transcripts (segments) and search chunks | 180 days | `teams.retention_transcript_days` |
| Recaps, action items, person updates (the whole meeting) | 2 years | `teams.retention_recap_days` |

A nightly job (`retention_purge`) enforces these. A Blob lifecycle rule also deletes media after 30 days, as a
backstop. Change a team's values with `PATCH /v1/admin/teams/{id}`.

## Handling

- Secrets come from the environment only (Key Vault in Azure).
- Transcript text is never logged at INFO level. Logs contain ids, counts and statuses.
- Graph notifications are accepted only with the configured `clientState`. Bot activities are accepted only with a
  valid Bot Framework token.
