/**
 * Cloudflare Worker: AmazonAffiliateBot Scheduled Publishing Trigger
 *
 * Replaces GitHub Actions native cron with highly reliable Cloudflare Worker Cron.
 * Triggers GitHub Actions workflow via repository_dispatch (POST /repos/{owner}/{repo}/dispatches).
 *
 * Environment variables / secrets:
 * - GITHUB_PAT (Secret, required): GitHub Personal Access Token with repo/actions write permissions.
 * - GITHUB_OWNER (Var, optional): GitHub repository owner (default: Peterson1979).
 * - GITHUB_REPO (Var, optional): GitHub repository name (default: AmazonAffiliateBot).
 * - WORKFLOW_FILE (Var, optional): Target workflow filename (default: scheduled_publishing.yml).
 * - EVENT_TYPE (Var, optional): repository_dispatch event type (default: cloudflare-scheduled-publish).
 * - CHECK_ACTIVE_RUNS (Var, optional): If "true", queries GitHub API to skip dispatch if a run is already active (default: true).
 * - TRIGGER_SECRET (Secret, optional): Shared secret token required for HTTP POST /trigger calls.
 */

/**
 * Returns formatted Europe/Budapest ISO-like datetime string.
 * @param {Date} date
 * @returns {string}
 */
export function getBudapestTimeStr(date = new Date()) {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Europe/Budapest",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date).replace(", ", "T");
}

/**
 * Check if a workflow run is already active (in_progress or queued) on GitHub.
 * @param {object} env
 * @returns {Promise<{ active: boolean, reason?: string, runId?: number }>}
 */
export async function checkActiveWorkflowRuns(env) {
  const owner = env.GITHUB_OWNER || "Peterson1979";
  const repo = env.GITHUB_REPO || "AmazonAffiliateBot";
  const workflow = env.WORKFLOW_FILE || "scheduled_publishing.yml";

  const url = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflow}/runs?status=in_progress&per_page=5`;

  try {
    const res = await fetch(url, {
      method: "GET",
      headers: {
        "Accept": "application/vnd.github+json",
        "Authorization": `Bearer ${env.GITHUB_PAT}`,
        "User-Agent": "Cloudflare-Worker-Publisher/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
      },
    });

    if (!res.ok) {
      console.warn(`[WARN] Active run check failed with status ${res.status}; continuing with dispatch.`);
      return { active: false };
    }

    const data = await res.json();
    const inProgressCount = data.total_count || (data.workflow_runs && data.workflow_runs.length) || 0;
    if (inProgressCount > 0) {
      const activeRun = data.workflow_runs && data.workflow_runs[0];
      return {
        active: true,
        reason: `A workflow run is already in progress (run ID: ${activeRun ? activeRun.id : "unknown"})`,
        runId: activeRun ? activeRun.id : undefined,
      };
    }

    // Also check queued runs
    const queuedUrl = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflow}/runs?status=queued&per_page=5`;
    const queuedRes = await fetch(queuedUrl, {
      method: "GET",
      headers: {
        "Accept": "application/vnd.github+json",
        "Authorization": `Bearer ${env.GITHUB_PAT}`,
        "User-Agent": "Cloudflare-Worker-Publisher/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
      },
    });

    if (queuedRes.ok) {
      const queuedData = await queuedRes.json();
      const queuedCount = queuedData.total_count || (queuedData.workflow_runs && queuedData.workflow_runs.length) || 0;
      if (queuedCount > 0) {
        const queuedRun = queuedData.workflow_runs && queuedData.workflow_runs[0];
        return {
          active: true,
          reason: `A workflow run is already queued (run ID: ${queuedRun ? queuedRun.id : "unknown"})`,
          runId: queuedRun ? queuedRun.id : undefined,
        };
      }
    }

    return { active: false };
  } catch (err) {
    console.warn(`[WARN] Failed to query active GitHub workflow runs: ${err.message}; continuing.`);
    return { active: false };
  }
}

/**
 * Dispatch repository_dispatch event to GitHub.
 * @param {object} env
 * @param {object} payload
 * @returns {Promise<{ ok: boolean, status: number, body?: string }>}
 */
export async function dispatchToGitHub(env, payload = {}) {
  const owner = env.GITHUB_OWNER || "Peterson1979";
  const repo = env.GITHUB_REPO || "AmazonAffiliateBot";
  const eventType = env.EVENT_TYPE || "cloudflare-scheduled-publish";

  if (!env.GITHUB_PAT) {
    throw new Error("GITHUB_PAT secret is missing. Configure it using 'wrangler secret put GITHUB_PAT'.");
  }

  const dispatchUrl = `https://api.github.com/repos/${owner}/${repo}/dispatches`;

  const bodyData = {
    event_type: eventType,
    client_payload: {
      source: "cloudflare-worker",
      timestamp: new Date().toISOString(),
      budapest_time: getBudapestTimeStr(),
      ...payload,
    },
  };

  const response = await fetch(dispatchUrl, {
    method: "POST",
    headers: {
      "Accept": "application/vnd.github+json",
      "Authorization": `Bearer ${env.GITHUB_PAT}`,
      "User-Agent": "Cloudflare-Worker-Publisher/1.0",
      "X-GitHub-Api-Version": "2022-11-28",
      "Content-Type": "application/json",
    },
    body: JSON.stringify(bodyData),
  });

  // GitHub returns 204 No Content on successful dispatch
  if (response.status === 204) {
    return { ok: true, status: response.status };
  }

  const resText = await response.text();
  return { ok: false, status: response.status, body: resText };
}

export default {
  /**
   * Cron Scheduled Handler
   */
  async scheduled(event, env, ctx) {
    const scheduledTime = new Date(event.scheduledTime || Date.now());
    const bpTime = getBudapestTimeStr(scheduledTime);

    console.log(`[INFO] Cloudflare Worker Cron trigger invoked.`);
    console.log(`[INFO] Scheduled time (UTC): ${scheduledTime.toISOString()} | Europe/Budapest: ${bpTime}`);
    console.log(`[INFO] Cron expression: ${event.cron || "unknown"}`);

    if (!env.GITHUB_PAT) {
      console.error(`[ERROR] GITHUB_PAT secret is not set. Cannot dispatch to GitHub.`);
      return;
    }

    // Check for active workflow runs to prevent overlapping runs if enabled
    if (env.CHECK_ACTIVE_RUNS !== "false") {
      const activeCheck = await checkActiveWorkflowRuns(env);
      if (activeCheck.active) {
        console.log(`[INFO] ${activeCheck.reason}. Skipping dispatch to avoid duplicate/overlapping execution.`);
        return;
      }
    }

    try {
      const result = await dispatchToGitHub(env, {
        trigger: "cron",
        cron: event.cron,
        scheduled_utc: scheduledTime.toISOString(),
      });

      if (result.ok) {
        console.log(`[SUCCESS] Successfully sent repository_dispatch to ${env.GITHUB_OWNER || "Peterson1979"}/${env.GITHUB_REPO || "AmazonAffiliateBot"}.`);
      } else {
        console.error(`[ERROR] GitHub dispatch API returned status ${result.status}: ${result.body}`);
      }
    } catch (err) {
      console.error(`[ERROR] Failed to dispatch scheduled publishing run: ${err.message}`);
    }
  },

  /**
   * HTTP Fetch Handler (Status check & Manual trigger)
   */
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname;
    const method = request.method.toUpperCase();

    // Health check / Status endpoint
    if (method === "GET" && (path === "/" || path === "/status" || path === "/health")) {
      const bpTime = getBudapestTimeStr();
      return new Response(
        JSON.stringify({
          status: "healthy",
          service: "AmazonAffiliateBot Cloudflare Publishing Trigger",
          target_repo: `${env.GITHUB_OWNER || "Peterson1979"}/${env.GITHUB_REPO || "AmazonAffiliateBot"}`,
          event_type: env.EVENT_TYPE || "cloudflare-scheduled-publish",
          workflow_file: env.WORKFLOW_FILE || "scheduled_publishing.yml",
          has_github_pat: Boolean(env.GITHUB_PAT),
          utc_time: new Date().toISOString(),
          budapest_time: bpTime,
        }, null, 2),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }
      );
    }

    // Manual / On-Demand Trigger endpoint
    if (method === "POST" && path === "/trigger") {
      // Authenticate if TRIGGER_SECRET is set
      if (env.TRIGGER_SECRET) {
        const authHeader = request.headers.get("Authorization") || "";
        const secretHeader = request.headers.get("X-Trigger-Secret") || "";
        const token = authHeader.startsWith("Bearer ") ? authHeader.substring(7).trim() : authHeader.trim();

        if (token !== env.TRIGGER_SECRET && secretHeader !== env.TRIGGER_SECRET) {
          return new Response(
            JSON.stringify({ error: "Unauthorized: Invalid or missing trigger secret." }),
            { status: 401, headers: { "Content-Type": "application/json" } }
          );
        }
      }

      let payload = {};
      try {
        const text = await request.text();
        if (text) {
          payload = JSON.parse(text);
        }
      } catch (e) {
        return new Response(
          JSON.stringify({ error: "Invalid JSON body." }),
          { status: 400, headers: { "Content-Type": "application/json" } }
        );
      }

      // Check active runs if not explicitly bypassed
      if (env.CHECK_ACTIVE_RUNS !== "false" && !payload.bypass_active_check) {
        const activeCheck = await checkActiveWorkflowRuns(env);
        if (activeCheck.active) {
          return new Response(
            JSON.stringify({
              success: false,
              message: activeCheck.reason,
              active_run_id: activeCheck.runId,
            }),
            { status: 409, headers: { "Content-Type": "application/json" } }
          );
        }
      }

      try {
        const result = await dispatchToGitHub(env, {
          trigger: "manual_http",
          ...payload,
        });

        if (result.ok) {
          return new Response(
            JSON.stringify({
              success: true,
              message: "repository_dispatch successfully delivered to GitHub.",
              target_repo: `${env.GITHUB_OWNER || "Peterson1979"}/${env.GITHUB_REPO || "AmazonAffiliateBot"}`,
              event_type: env.EVENT_TYPE || "cloudflare-scheduled-publish",
              timestamp: new Date().toISOString(),
              budapest_time: getBudapestTimeStr(),
            }),
            { status: 200, headers: { "Content-Type": "application/json" } }
          );
        } else {
          return new Response(
            JSON.stringify({
              success: false,
              status: result.status,
              error: result.body,
            }),
            { status: 502, headers: { "Content-Type": "application/json" } }
          );
        }
      } catch (err) {
        return new Response(
          JSON.stringify({ success: false, error: err.message }),
          { status: 500, headers: { "Content-Type": "application/json" } }
        );
      }
    }

    return new Response(
      JSON.stringify({ error: "Not Found", path }),
      { status: 404, headers: { "Content-Type": "application/json" } }
    );
  },
};

