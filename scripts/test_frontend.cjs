/* Package-free browser utility checks: node --test scripts/test_frontend.cjs. */
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const { webcrypto } = require("node:crypto");
const test = require("node:test");
const assert = require("node:assert/strict");

const source = fs.readFileSync(
  path.join(__dirname, "../api_watch/static/app.js"),
  "utf8",
);

function createClient(compatibility = false, viewportWidth = 930) {
  let now = Date.UTC(2026, 9, 19, 12, 0, 0);
  class ClientDate extends Date {
    constructor(...args) {
      super(...(args.length ? args : [now]));
    }
    static now() {
      return now;
    }
  }
  const input = { focus() {} };
  const modal = {
    open: false,
    innerHTML: "",
    querySelector() {
      return input;
    },
    addEventListener() {},
    showModal() {
      this.open = true;
    },
    close() {
      this.open = false;
    },
    replaceChildren() {},
  };
  const root = { innerHTML: "", clientWidth: viewportWidth };
  const location = {
    hash: "",
    origin: "http://localhost",
    pathname: "/",
    search: "",
  };
  const context = {
    document: {
      querySelector(selector) {
        return selector === "#app" || selector === "#main" ? root : modal;
      },
      addEventListener() {},
      activeElement: null,
    },
    window: { addEventListener() {} },
    crypto: webcrypto,
    URL,
    URLSearchParams,
    TextEncoder,
    Intl,
    Date: ClientDate,
    location,
    history: {
      replaceState(_state, _title, url) {
        location.hash = url.slice(url.indexOf("#"));
      },
    },
    requestAnimationFrame(callback) {
      callback();
    },
    setInterval() {},
    setTimeout() {},
    console,
  };
  if (compatibility) {
    context.JSON = {
      stringify: JSON.stringify,
      parse(text, reviver) {
        return JSON.parse(text, function (key, value) {
          return reviver ? reviver.call(this, key, value) : value;
        });
      },
    };
  }
  vm.createContext(context);
  // Exercise the same private functions as the browser without adding globals
  // or test hooks to the shipped application and without initiating requests.
  const initialization =
    [...source.matchAll(/^\s*\(async\s*\(\)\s*=>/gm)].at(-1)?.index ?? -1;
  assert.ok(initialization > 0, "Browser initialization boundary must exist");
  vm.runInContext(
    source.slice(0, initialization) +
      "globalThis.testClient={parseExact,serialize,json,JsonLiteral,period,state,monitorForm,authLocation,renderAuth,pruneUnchangedExecution,incidentPeriod,observationCoverage,latencySummary,renderMonitorResults,date,openIncidentMarker,renderChart,shell};})();",
    context,
  );
  return {
    client: context.testClient,
    modal,
    root,
    location,
    clock: {
      advance(ms) {
        now += ms;
      },
    },
  };
}

for (const compatibility of [false, true]) {
  const mode = compatibility ? "compatibility parser" : "native parser";
  test(`Configuration numbers roundtrip without precision loss (${mode})`, () => {
    const { client } = createClient(compatibility);
    const raw =
      '{"body":{"json":{"large":9007199254740993123456789,"precise":1.0000000000000000000000001}},"assertion":{"expected":0.12345678901234567890123456789}}';
    const value = client.parseExact(raw);
    assert.equal(client.serialize(value), raw);
    assert.match(client.json(value.body.json), /9007199254740993123456789/);
    assert.equal(
      client.json(value.assertion.expected, value.assertion, "expected"),
      "0.12345678901234567890123456789",
    );
  });

  test(`JSON strings, escapes, and arrays remain intact (${mode})`, () => {
    const { client } = createClient(compatibility);
    const raw =
      '{"text":"123 \\" inside","array":[-1.2300e+25,0,true,null,{"x":12345678901234567890}]}';
    assert.equal(client.serialize(client.parseExact(raw)), raw);
    assert.throws(() => client.parseExact('{"invalid":01}'));
    assert.throws(() => client.parseExact('{"invalid":NaN}'));
  });
}

test("User-entered JSON preserves numeric literals and rejects invalid input", () => {
  const { client } = createClient();
  assert.equal(
    client.serialize({
      body: new client.JsonLiteral('{"x":999999999999999999999}', "Body"),
    }),
    '{"body":{"x":999999999999999999999}}',
  );
  assert.throws(
    () => new client.JsonLiteral("{invalid}", "Body"),
    /valid JSON/,
  );
  assert.throws(() => new client.JsonLiteral("NaN", "Body"), /valid JSON/);
});

test("Paginated checks retain the same period bounds across requests", () => {
  const { client, clock } = createClient();
  const first = client.period();
  clock.advance(20000);
  const next = client.period();
  assert.equal(first.from, next.from);
  assert.equal(first.to, next.to);
  client.state.checkFilters.config_version = "2";
  assert.equal(client.period().config_version, "2");
  assert.equal(client.period().from, first.from);
});

test("Incident pagination freezes its period until a new filter or refresh", () => {
  const { client, clock } = createClient();
  assert.equal(Object.keys(client.incidentPeriod()).length, 0);
  client.state.incidentRange = "24h";
  const first = client.incidentPeriod();
  clock.advance(30000);
  assert.equal(client.incidentPeriod().from, first.from);
  assert.equal(client.incidentPeriod().to, first.to);
  client.state.incidentBounds = null;
  assert.notEqual(client.incidentPeriod().to, first.to);
  client.state.incidentRange = "custom";
  client.state.incidentFrom = "2026-10-01T10:00";
  client.state.incidentTo = "2026-10-02T10:00";
  client.state.incidentBounds = null;
  assert.equal(
    new Date(client.incidentPeriod().to) -
      new Date(client.incidentPeriod().from),
    86400000,
  );
});

test("Coverage exposes all unevaluated outcomes and latency never invents zero samples", () => {
  const { client } = createClient();
  const coverage = client.observationCoverage({
    blocked_count: 2,
    interrupted_count: 3,
    cancelled_count: 4,
    skipped_slot_count: 7,
  });
  for (const label of ["Blocked", "Interrupted", "Cancelled", "Skipped slots"])
    assert.ok(coverage.includes(label));
  for (const count of [2, 3, 4, 7])
    assert.ok(coverage.includes("<dd>" + count + "</dd>"));
  const latency = client.latencySummary({
    min_response_time_ms: null,
    p50_response_time_ms: null,
    max_response_time_ms: null,
    latency_sample_count: 0,
  });
  assert.equal((latency.match(/<dd>—<\/dd>/g) || []).length, 3);
  assert.doesNotMatch(latency, /0 ms/);
  assert.match(client.date("2026-10-19T12:00:00Z"), /Oct/);
});

test("Awaiting-data status filter excludes paused monitors", () => {
  const { client, modal } = createClient();
  const monitor = {
    url: "https://example.com/",
    method: "GET",
    interval_seconds: 60,
    last_checked_at: null,
    last_response_time_ms: null,
  };
  client.state.monitors = [
    {
      ...monitor,
      id: "paused",
      name: "Paused endpoint",
      state: "paused",
      health_status: "unknown",
    },
    {
      ...monitor,
      id: "active",
      name: "New active endpoint",
      state: "active",
      health_status: "unknown",
    },
  ];
  client.state.monitorStatus = "unknown";
  client.renderMonitorResults();
  assert.match(modal.innerHTML, /New active endpoint/);
  assert.doesNotMatch(modal.innerHTML, /Paused endpoint/);
});

test("Healthy and Down filter labels select the API up and down states", () => {
  const { client, modal } = createClient();
  const monitor = {
    url: "https://example.com/",
    method: "GET",
    interval_seconds: 60,
    last_checked_at: null,
    last_response_time_ms: null,
  };
  client.state.monitors = [
    {
      ...monitor,
      id: "up",
      name: "Passing endpoint",
      state: "active",
      health_status: "up",
    },
    {
      ...monitor,
      id: "down",
      name: "Failing endpoint",
      state: "active",
      health_status: "down",
    },
    {
      ...monitor,
      id: "paused",
      name: "Paused prior success",
      state: "paused",
      health_status: "up",
    },
    {
      ...monitor,
      id: "recovering",
      name: "Recovering endpoint",
      state: "active",
      health_status: "recovering",
    },
  ];
  for (const [filter, included, excluded] of [
    ["healthy", "Passing endpoint", "Failing endpoint"],
    ["up", "Passing endpoint", "Failing endpoint"],
    ["unhealthy", "Failing endpoint", "Passing endpoint"],
    ["down", "Failing endpoint", "Passing endpoint"],
  ]) {
    client.state.monitorStatus = filter;
    client.renderMonitorResults();
    assert.ok(
      modal.innerHTML.includes(included),
      `${filter} must select its actual API health value`,
    );
    assert.ok(!modal.innerHTML.includes(excluded));
    assert.doesNotMatch(
      modal.innerHTML,
      /Paused prior success|Recovering endpoint/,
    );
    assert.match(modal.innerHTML, /1 monitor<\/span>/);
  }
});

test("An open incident remains visible while health is unknown or paused", () => {
  const { client } = createClient();
  for (const state of ["active", "paused"]) {
    const marker = client.openIncidentMarker({
      state,
      health_status: "unknown",
      open_incident_id: "incident-id",
      name: "Endpoint",
    });
    assert.match(marker, /Open incident/);
    assert.match(marker, /data-id="incident-id"/);
  }
  assert.equal(client.openIncidentMarker({ open_incident_id: null }), "");
});

test("Monitor forms escape remote names, descriptions, and request values", () => {
  const { client, modal } = createClient();
  client.monitorForm({
    id: "test",
    name: "<img src=x onerror=alert(1)>",
    description: "</textarea><script>bad()</script>",
    url: "https://example.com/",
    state: "paused",
    method: "GET",
    interval_seconds: 60,
    timeout_ms: 10000,
    expected_status: 200,
    max_response_time_ms: null,
    failure_threshold: 3,
    recovery_threshold: 2,
    body: { enabled: false },
    headers: [],
    secret_headers: [],
    query_params: [
      { name: '" autofocus onfocus="bad()', value: "<script>bad()</script>" },
    ],
    json_assertions: [],
    auth: { type: "none" },
  });
  assert.doesNotMatch(modal.innerHTML, /<img src=x|<script>bad/);
  assert.match(modal.innerHTML, /&lt;img/);
  assert.match(modal.innerHTML, /autocomplete="url"/);
});

test("Password reset links are consumed into memory and stripped from the address", () => {
  const { client, location, root } = createClient();
  location.hash = "#reset=one-time-secret";
  assert.equal(client.authLocation(), "reset");
  assert.equal(location.hash, "#reset");
  client.renderAuth("reset");
  assert.match(root.innerHTML, /value="one-time-secret"/);
});

test("Metadata edits preserve request configuration and existing secrets", () => {
  const { client } = createClient();
  const monitor = client.parseExact(
    '{"method":"GET","auth":{"type":"bearer","configured":true},"body":{"enabled":true,"json":{"large":9007199254740993123456789}},"json_assertions":[{"pointer":"/number","operator":"equals","expected":1.000000000000000000000001}]}',
  );
  const patch = {
    name: "New name",
    description: "",
    notifications_enabled: true,
    method: "GET",
    auth: { type: "bearer" },
    secret_headers: [],
    body: {
      enabled: true,
      json: new client.JsonLiteral(client.json(monitor.body.json), "Body"),
    },
    json_assertions: [
      {
        pointer: "/number",
        operator: "equals",
        expected: new client.JsonLiteral(
          client.json(
            monitor.json_assertions[0].expected,
            monitor.json_assertions[0],
            "expected",
          ),
          "Expected",
        ),
      },
    ],
  };
  client.pruneUnchangedExecution(patch, monitor);
  assert.deepEqual(Object.keys(patch).sort(), [
    "description",
    "name",
    "notifications_enabled",
  ]);
  const secretPatch = {
    auth: { type: "bearer", token: "replacement" },
    secret_headers: [{ name: "X-Key", remove: true }],
  };
  client.pruneUnchangedExecution(secretPatch, monitor);
  assert.equal(secretPatch.auth.token, "replacement");
  assert.equal(secretPatch.secret_headers[0].remove, true);
});

test("Application markup is compatible with the self-only Content Security Policy", () => {
  assert.doesNotMatch(source, /style="|onclick="|onsubmit="/);
});

test("Charts fit narrow panels and leave missing observations disconnected", () => {
  const points = [10, 20, null, 30].map((value, index) => ({
    bucket_start: new Date(Date.UTC(2026, 9, 19, 12, index)).toISOString(),
    bucket_end: new Date(Date.UTC(2026, 9, 19, 12, index + 1)).toISOString(),
    avg_response_time_ms: value,
  }));
  for (const width of [360, 700, 1300]) {
    const { client } = createClient(false, width);
    const markup = client.renderChart({ items: points });
    const canvasWidth = Number(markup.match(/viewBox="0 0 (\d+) /)[1]);
    assert.ok(
      canvasWidth <= width,
      "SVG coordinates must match the visible panel, keeping text readable",
    );
    assert.equal(
      (markup.match(/class="line"/g) || []).length,
      2,
      "A missing sample must split the plotted line",
    );
  }
});

test("Latency and availability charts retain distinct semantic color roles", () => {
  const { client } = createClient();
  const points = [10, 20].map((value, index) => ({
    bucket_start: new Date(Date.UTC(2026, 9, 19, 12, index)).toISOString(),
    bucket_end: new Date(Date.UTC(2026, 9, 19, 12, index + 1)).toISOString(),
    avg_response_time_ms: value,
    success_rate_percent: index ? 100 : 50,
  }));
  const latency = client.renderChart({ items: points });
  const availability = client.renderChart(
    { items: points },
    "success_rate_percent",
    true,
  );
  assert.match(latency, /class="chart chart-latency"/);
  assert.match(latency, /stop-color="var\(--accent\)"/);
  assert.doesNotMatch(latency, /stop-color="var\(--green\)"/);
  assert.match(latency, /aria-label="Average response time[^\"]*milliseconds/);
  assert.match(availability, /class="chart chart-rate"/);
  assert.match(availability, /stop-color="var\(--green\)"/);
  assert.doesNotMatch(availability, /stop-color="var\(--accent\)"/);
  assert.match(
    availability,
    /aria-label="Successful scheduled checks as a percentage/,
  );
});

test("The application shell exposes a header navigation and current route", () => {
  const { client, root } = createClient();
  client.state.user = { email: "review@example.com" };
  for (const [route, expected] of [
    ["overview", "overview"],
    ["monitors", "monitors"],
    ["monitor/endpoint-id", "monitors"],
    ["incidents", "incidents"],
    ["settings", "settings"],
  ]) {
    client.shell(route);
    assert.doesNotMatch(root.innerHTML, /<aside\b|class="sidebar(?:\s|")/);
    const header = root.innerHTML.match(/<header\b[^>]*>([\s\S]*?)<\/header>/);
    assert.ok(
      header,
      "Primary navigation belongs in the top-level application header",
    );
    const nav = header[1].match(/<nav\b([^>]*)>([\s\S]*?)<\/nav>/);
    assert.ok(nav);
    assert.match(nav[1], /\bid="primary-navigation"/);
    assert.match(nav[1], /\baria-label="[^"]+"/);
    const current = [...nav[2].matchAll(/<a\b([^>]*)>/g)]
      .map((match) => match[1])
      .filter((attributes) => /\baria-current="page"/.test(attributes));
    assert.equal(
      current.length,
      1,
      "Exactly one navigation link identifies the current page",
    );
    assert.match(current[0], new RegExp(`\\bhref="#${expected}"`));
    assert.match(header[1], /\baria-controls="primary-navigation"/);
    assert.match(header[1], /\baria-expanded="false"/);
    assert.match(root.innerHTML, /<main\b[^>]*\bid="main"/);
  }
});
