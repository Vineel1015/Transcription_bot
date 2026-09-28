# Teams setup

This gets a tenant from nothing to a working bot. Steps 1–2 are automated by `uv run recall setup-teams`, which
uses the `az` CLI and prints the rest with your values filled in.

## Roles you need

| Step | Role |
|---|---|
| App registration, client secret | Application Developer (or higher) |
| Admin consent for Graph application permissions | Privileged Role Administrator or Global Administrator |
| Application access policy, transcription policy | Teams Administrator |
| Uploading the Teams app for everyone | Teams Administrator (or allow custom app uploads) |

## 1. App registration (one app for Graph and the bot)

```bash
az login
uv run recall setup-teams
```

The script:

1. Creates a single-tenant app registration called **Recall**.
2. Adds these Microsoft Graph **application** permissions:

   | Permission | Why |
   |---|---|
   | `OnlineMeetings.Read.All` | Meeting subject, times, join URL, participants |
   | `OnlineMeetingTranscript.Read.All` | Transcript metadata and content, and the transcript subscription |
   | `User.Read.All` | Map participants to people |

3. Creates a client secret.

Store the secret in Key Vault (Azure) or `.env` (local), as `GRAPH_CLIENT_SECRET` and `BOT_APP_PASSWORD`.

## 2. Admin consent

```bash
az ad app permission admin-consent --id <app-id>
```

Or: Entra admin center → App registrations → Recall → API permissions → *Grant admin consent*.

## 3. Application access policy (Teams PowerShell)

App-only access to online meetings needs an access policy. Without one, Graph returns 403 for meeting and transcript reads.

```powershell
Install-Module MicrosoftTeams; Connect-MicrosoftTeams
New-CsApplicationAccessPolicy -Identity Recall-Policy -AppIds "<app-id>" -Description "Recall reads meeting transcripts"
Grant-CsApplicationAccessPolicy -PolicyName Recall-Policy -Global
# or per organizer: Grant-CsApplicationAccessPolicy -PolicyName Recall-Policy -Identity hannah.lee@contoso.com
```

A policy can take up to 30 minutes to apply.

## 4. Turn on transcription

In the Teams admin center, go to **Meetings → Meeting policies → (your policy) → Recording & transcription →
Transcription: On**. Transcription must then be started in each meeting. Organizers can set recurring standups to
start transcription automatically (*Meeting options → Record and transcribe automatically*). Recall only sees meetings
that have a transcript.

Also check that your tenant hasn't turned off Graph access to transcripts. If it has, subscriptions fail with
`403 GraphAccessToTranscriptsDisabled`.

## 5. Webhook and bot endpoint

Graph and the Bot Framework need a public HTTPS URL.

- **Azure:** `azd up` prints `SERVICE_APP_ENDPOINT_URL`, `GRAPH_WEBHOOK_URL` and `BOT_MESSAGING_ENDPOINT`, and creates the
  Azure Bot resource with the Teams channel when `BOT_APP_ID` is set (`azd env set BOT_APP_ID <app-id>`).
- **Local development with a tunnel:**
  ```bash
  devtunnel host -p 8000 --allow-anonymous      # or: ngrok http 8000
  ```
  Set `PUBLIC_BASE_URL=https://<tunnel-host>` in `.env` and restart. Create an Azure Bot resource
  (single tenant, the same app id) with messaging endpoint `https://<tunnel-host>/api/messages`, and enable the Teams
  channel.

On startup, and every hour after that, Recall creates or renews the tenant-wide subscription
`communications/onlineMeetings/getAllTranscripts`. The subscription answers Graph's validation handshake at
`/graph/notifications`. Notifications only fire for meetings whose transcription **starts after** the subscription
exists. The 15-minute poll (`GRAPH_POLL_ORGANIZERS`) catches anything missed.

## 6. Tell Recall about your teams

Create teams and people with the admin API. The seed script shows the fields; people who message the bot are
created automatically.

```bash
curl -X POST $URL/v1/admin/teams -H "Authorization: Bearer $SERVICE_TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "Platform", "channel_id": "19:…@thread.tacv2", "timezone": "America/New_York",
       "standup_time": "09:30", "title_patterns": ["platform standup", "platform planning"]}'
```

Each transcript is mapped to a team by the meeting's join URL (`join_urls`) or by a title regex (`title_patterns`).
Transcripts that don't match any team are skipped. To find `channel_id`, use *Get link to channel* in Teams and take
the `19:…@thread.tacv2` part.

## 7. Upload the Teams app

```bash
BOT_APP_ID=<app-id> PUBLIC_BASE_URL=https://<host> uv run recall teams-package
```

Upload `dist/recall-teams-app.zip` in Teams → *Apps → Manage your apps → Upload an app* (or through the Teams
admin center for the whole org). Then add it to the team's channel.
