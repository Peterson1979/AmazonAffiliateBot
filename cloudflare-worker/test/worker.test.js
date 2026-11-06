import test from "node:test";
import assert from "node:assert/strict";
import worker, {
  getBudapestTimeStr,
  getBudapestTimeParts,
  isBudapestPublishingWindow,
  checkActiveWorkflowRuns,
  dispatchToGitHub,
} from "../src/index.js";

test("getBudapestTimeStr formats Budapest time correctly", () => {
  const d = new Date("2026-11-06T14:00:00Z");
  const bpStr = getBudapestTimeStr(d);
  // In November (CET, UTC+1), 14:00 UTC is 15:00 CET
  assert.match(bpStr, /^2026-11-06T15:00:00/);
});

test("isBudapestPublishingWindow validates all 4 windows in CET and CEST", () => {
  // CET (Winter, UTC+1): 14:00Z = 15:00, 17:00Z = 18:00, 20:00Z = 21:00, 22:00Z = 23:00
  assert.equal(isBudapestPublishingWindow(new Date("2026-12-15T14:00:00Z")), true, "CET 15:00");
  assert.equal(isBudapestPublishingWindow(new Date("2026-12-15T17:00:00Z")), true, "CET 18:00");
  assert.equal(isBudapestPublishingWindow(new Date("2026-12-15T20:00:00Z")), true, "CET 21:00");
  assert.equal(isBudapestPublishingWindow(new Date("2026-12-15T22:00:00Z")), true, "CET 23:00");

  // CEST (Summer, UTC+2): 13:00Z = 15:00, 16:00Z = 18:00, 19:00Z = 21:00, 21:00Z = 23:00
  assert.equal(isBudapestPublishingWindow(new Date("2026-07-15T13:00:00Z")), true, "CEST 15:00");
  assert.equal(isBudapestPublishingWindow(new Date("2026-07-15T16:00:00Z")), true, "CEST 18:00");
  assert.equal(isBudapestPublishingWindow(new Date("2026-07-15T19:00:00Z")), true, "CEST 21:00");
  assert.equal(isBudapestPublishingWindow(new Date("2026-07-15T21:00:00Z")), true, "CEST 23:00");

  // Out-of-season triggers resulting from UTC unions:
  // In CET (winter): 13:00 UTC = 14:00 CET (not in windows 15, 18, 21, 23)
  assert.equal(isBudapestPublishingWindow(new Date("2026-12-15T13:00:00Z")), false, "CET 14:00 out-of-season");
  // In CEST (summer): 14:00 UTC = 16:00 CEST (not in windows 15, 18, 21, 23)
  assert.equal(isBudapestPublishingWindow(new Date("2026-07-15T14:00:00Z")), false, "CEST 16:00 out-of-season");
  // In CEST (summer): 17:00 UTC = 19:00 CEST (not in windows 15, 18, 21, 23)
  assert.equal(isBudapestPublishingWindow(new Date("2026-07-15T17:00:00Z")), false, "CEST 19:00 out-of-season");
});

test("dispatchToGitHub makes valid repository_dispatch request to GitHub", async () => {
  let capturedUrl = "";
  let capturedOptions = {};

  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    capturedUrl = url;
    capturedOptions = options;
    return new Response(null, { status: 204 });
  };

  try {
    const env = {
      GITHUB_PAT: "test_pat_token_123",
      GITHUB_OWNER: "Peterson1979",
      GITHUB_REPO: "AmazonAffiliateBot",
      EVENT_TYPE: "cloudflare-scheduled-publish",
    };

    const res = await dispatchToGitHub(env, { test_key: "test_val" });
    assert.equal(res.ok, true);
    assert.equal(res.status, 204);
    assert.equal(
      capturedUrl,
      "https://api.github.com/repos/Peterson1979/AmazonAffiliateBot/dispatches"
    );
    assert.equal(capturedOptions.method, "POST");
    assert.equal(capturedOptions.headers["Authorization"], "Bearer test_pat_token_123");
    assert.equal(capturedOptions.headers["Accept"], "application/vnd.github+json");

    const sentBody = JSON.parse(capturedOptions.body);
    assert.equal(sentBody.event_type, "cloudflare-scheduled-publish");
    assert.equal(sentBody.client_payload.source, "cloudflare-worker");
    assert.equal(sentBody.client_payload.test_key, "test_val");
    assert.ok(sentBody.client_payload.budapest_time);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("checkActiveWorkflowRuns detects in_progress runs", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    return new Response(
      JSON.stringify({
        total_count: 1,
        workflow_runs: [{ id: 99887766, status: "in_progress" }],
      }),
      { status: 200, headers: { "Content-Type": "application/json" } }
    );
  };

  try {
    const env = {
      GITHUB_PAT: "dummy_pat",
      GITHUB_OWNER: "Peterson1979",
      GITHUB_REPO: "AmazonAffiliateBot",
    };

    const check = await checkActiveWorkflowRuns(env);
    assert.equal(check.active, true);
    assert.equal(check.runId, 99887766);
    assert.match(check.reason, /already in progress/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("scheduled handler skips dispatch if active run is in progress", async () => {
  let dispatchCalled = false;

  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    if (url.includes("/actions/workflows/")) {
      return new Response(
        JSON.stringify({
          total_count: 1,
          workflow_runs: [{ id: 554433, status: "in_progress" }],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      );
    }
    if (url.includes("/dispatches")) {
      dispatchCalled = true;
      return new Response(null, { status: 204 });
    }
    return new Response(null, { status: 404 });
  };

  try {
    const env = {
      GITHUB_PAT: "test_pat",
      CHECK_ACTIVE_RUNS: "true",
    };
    // 14:00 UTC = 15:00 CET (active window)
    const event = { cron: "0 14 * * *", scheduledTime: new Date("2026-11-06T14:00:00Z").getTime() };

    await worker.scheduled(event, env, {});
    assert.equal(dispatchCalled, false, "Should NOT dispatch when active run exists");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("scheduled handler executes dispatch when no runs are active", async () => {
  let dispatchCalled = false;

  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    if (url.includes("/actions/workflows/")) {
      return new Response(
        JSON.stringify({ total_count: 0, workflow_runs: [] }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      );
    }
    if (url.includes("/dispatches")) {
      dispatchCalled = true;
      return new Response(null, { status: 204 });
    }
    return new Response(null, { status: 404 });
  };

  try {
    const env = {
      GITHUB_PAT: "test_pat",
      CHECK_ACTIVE_RUNS: "true",
    };
    // 14:00 UTC = 15:00 CET (active window)
    const event = { cron: "0 14 * * *", scheduledTime: new Date("2026-11-06T14:00:00Z").getTime() };

    await worker.scheduled(event, env, {});
    assert.equal(dispatchCalled, true, "Should dispatch when no active run exists");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("scheduled handler drops out-of-season triggers without dispatching", async () => {
  let fetchCalled = false;

  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    fetchCalled = true;
    return new Response(null, { status: 200 });
  };

  try {
    const env = {
      GITHUB_PAT: "test_pat",
      CHECK_ACTIVE_RUNS: "true",
    };
    // In CET (winter): 13:00 UTC is 14:00 CET (out of season, not in [15, 18, 21, 23])
    const event = { cron: "0 13 * * *", scheduledTime: new Date("2026-12-15T13:00:00Z").getTime() };

    await worker.scheduled(event, env, {});
    assert.equal(fetchCalled, false, "Should drop out-of-season trigger without calling GitHub API");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("fetch GET / returns health status JSON", async () => {
  const req = new Request("https://worker.test/", { method: "GET" });
  const env = {
    GITHUB_OWNER: "Peterson1979",
    GITHUB_REPO: "AmazonAffiliateBot",
    GITHUB_PAT: "secret_pat",
  };

  const res = await worker.fetch(req, env, {});
  assert.equal(res.status, 200);
  const data = await res.json();
  assert.equal(data.status, "healthy");
  assert.equal(data.target_repo, "Peterson1979/AmazonAffiliateBot");
  assert.equal(data.has_github_pat, true);
  assert.ok(data.budapest_time);
});

test("fetch POST /trigger enforces TRIGGER_SECRET authorization", async () => {
  const env = {
    GITHUB_PAT: "test_pat",
    TRIGGER_SECRET: "super-secret-key-123",
  };

  // Unauthorized call without header
  const unauthReq = new Request("https://worker.test/trigger", { method: "POST" });
  const unauthRes = await worker.fetch(unauthReq, env, {});
  assert.equal(unauthRes.status, 401);

  // Authorized call
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    if (url.includes("/actions/workflows/")) {
      return new Response(
        JSON.stringify({ total_count: 0, workflow_runs: [] }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      );
    }
    if (url.includes("/dispatches")) {
      return new Response(null, { status: 204 });
    }
    return new Response(null, { status: 404 });
  };

  try {
    const authReq = new Request("https://worker.test/trigger", {
      method: "POST",
      headers: {
        "Authorization": "Bearer super-secret-key-123",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ dry_run: true }),
    });

    const authRes = await worker.fetch(authReq, env, {});
    assert.equal(authRes.status, 200);
    const data = await authRes.json();
    assert.equal(data.success, true);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

