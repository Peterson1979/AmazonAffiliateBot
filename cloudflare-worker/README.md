# Cloudflare Worker Cron Publishing Trigger

Cloudflare Worker Cron trigger for the **AmazonAffiliateBot** scheduled publishing workflow.

## Overview

GitHub Actions native cron triggers (`on.schedule`) run on a best-effort basis and can experience severe delays (30–90+ minutes) or get dropped entirely during peak traffic periods.

This Cloudflare Worker provides a **zero-infrastructure, high-reliability external scheduler**. It fires periodically via Cloudflare Worker Cron and dispatches a GitHub Actions workflow execution via GitHub's `repository_dispatch` API (`POST /repos/{owner}/{repo}/dispatches`).

### Key Features & Safety Guarantees

1. **High-Reliability Scheduler:** Cloudflare Worker Cron executes reliably at scheduled minute intervals without GitHub's queue delays.
2. **Duplicate Run & Overlap Protection:**
   - **Layer 1 (Worker level):** Checks the GitHub Actions API before dispatching; if a workflow run is already `in_progress` or `queued`, the worker logs and skips dispatch.
   - **Layer 2 (Workflow level):** Workflow uses `concurrency: group: publishing-queue-lock, cancel-in-progress: false`, ensuring runs never execute in parallel.
   - **Layer 3 (Runner level):** Runner uses atomic, evidence-backed slot claims (`claim_due_slots()`) persisted directly to Git before any publishing adapter is invoked.
3. **Timezone & Daylight Saving Alignment:**
   - The bot publishes across 4 daily windows in **Europe/Budapest** time:
     - **Window 1 (15:00):** Daily Slot 1 (Image) -> Facebook, Threads, Pinterest (`dailyideasforyou`)
     - **Window 2 (18:00):** Daily Slot 1 (Image) -> Pinterest (`LifeModeHQ`)
     - **Window 3 (21:00):** Daily Slot 2 (Video) -> Facebook, Threads, Pinterest (`dailyideasforyou`)
     - **Window 4 (23:00):** Daily Slot 2 (Video) -> Pinterest (`LifeModeHQ`)
   - Budapest observes **CET** (UTC+1, winter) and **CEST** (UTC+2, summer).
   - The worker cron is configured to run at `0,15 13,14,16,17,19,20,21,22 * * *`:
     - Summer (CEST, UTC+2): 13:00 UTC = 15:00 CEST (W1), 16:00 UTC = 18:00 CEST (W2), 19:00 UTC = 21:00 CEST (W3), 21:00 UTC = 23:00 CEST (W4).
     - Winter (CET, UTC+1): 14:00 UTC = 15:00 CET (W1), 17:00 UTC = 18:00 CET (W2), 20:00 UTC = 21:00 CET (W3), 22:00 UTC = 23:00 CET (W4).
     - The `:15` runs provide automatic retry/catch-up within the valid window if a transient network error occurred.
     - Out-of-season triggers resulting from UTC unions are automatically dropped inside worker code without dispatching.
4. **Preserved Queue & Overdue Semantics:**
   - Overdue items from previous Budapest calendar days remain held (`overdue_held`) and are **never** burst-published by scheduled triggers.
   - Live social media posts are sent only when inside the valid publishing window with a persisted slot claim.

---

## Setup Instructions

### 1. Create a GitHub Personal Access Token (PAT)

The Cloudflare Worker requires a GitHub Personal Access Token to authenticate against the GitHub API.

**Option A: Fine-Grained Personal Access Token (Recommended)**
1. In GitHub, go to **Settings** -> **Developer settings** -> **Personal access tokens** -> **Fine-grained tokens**.
2. Click **Generate new token**.
3. Name: `Cloudflare-Publishing-Cron`.
4. Expiration: Select desired duration (e.g. 1 year or custom).
5. Repository access: Select **Only select repositories** -> choose `Peterson1979/AmazonAffiliateBot`.
6. Permissions:
   - **Actions**: `Read and write` (needed to query workflow runs and dispatch).
   - **Contents**: `Read and write` (needed for repository dispatches).
7. Generate token and copy the value.

**Option B: Classic Personal Access Token**
1. Go to **Personal access tokens** -> **Tokens (classic)**.
2. Generate token with `repo` and `workflow` scopes.

---

### 2. Configure Cloudflare Worker Secrets

From your local machine or terminal in the `cloudflare-worker/` directory:

1. Install Wrangler (if not already installed globally) and log in:
   ```bash
   npx wrangler login
   ```

2. Set the `GITHUB_PAT` secret:
   ```bash
   npx wrangler secret put GITHUB_PAT
   ```
   Paste your GitHub Personal Access Token when prompted.

3. (Optional) Set a `TRIGGER_SECRET` to protect the manual HTTP trigger endpoint:
   ```bash
   npx wrangler secret put TRIGGER_SECRET
   ```
   Enter a secure random string (e.g., generated with `openssl rand -hex 16`).

---

### 3. Deploy the Worker

Deploy to your Cloudflare account:

```bash
cd cloudflare-worker
npx wrangler deploy
```

Wrangler will output the deployment URL, for example:
`https://amazon-affiliate-publishing-cron.<your-subdomain>.workers.dev`

---

## Verifying & Testing

### 1. Health & Status Check
Query the status endpoint in your browser or with curl:
```bash
curl https://amazon-affiliate-publishing-cron.<your-subdomain>.workers.dev/status
```
Response:
```json
{
  "status": "healthy",
  "service": "AmazonAffiliateBot Cloudflare Publishing Trigger",
  "target_repo": "Peterson1979/AmazonAffiliateBot",
  "event_type": "cloudflare-scheduled-publish",
  "workflow_file": "scheduled_publishing.yml",
  "has_github_pat": true,
  "utc_time": "2026-11-06T15:00:00.000Z",
  "budapest_time": "2026-11-06T16:00:00"
}
```

### 2. Manual Dry-Run Test via HTTP
You can test dispatching without sending live social posts by sending a dry-run payload:
```bash
curl -X POST https://amazon-affiliate-publishing-cron.<your-subdomain>.workers.dev/trigger \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <YOUR_TRIGGER_SECRET>" \
  -d '{"dry_run": true}'
```

### 3. Live Logs Streaming
To view cron invocations in real time:
```bash
npx wrangler tail
```

---

## Recovering Missed Past Items

If a past scheduled item (such as `item_030_B07PBXXNCY` from November 5, 21:00) was held because its scheduled calendar day passed:
- Scheduled automation **will not** publish past-day items (preventing spam/burst posts).
- To publish safely:
  1. Open the local dashboard (`http://127.0.0.1:5000/queue`).
  2. Locate the item marked **OVERDUE HELD**.
  3. Either click **Publish Now** (with live confirmation) to publish immediately, or click **Reschedule** to place it into the next available future slot.

