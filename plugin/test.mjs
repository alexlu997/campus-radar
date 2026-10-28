import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";
import { apply, createHandler, fields, runtimeConfig } from "./index.js";

const config = { python: "/usr/bin/false", repoRoot: "/tmp", dbPath: "/tmp/not-used.sqlite3" };
const request = (payload = {}, method = "campus-radar/list") => new Request("http://localhost/api/campus-radar/list", {
  method: "POST", headers: { "content-type": "application/json" },
  body: JSON.stringify({ type: "client-request", rpcId: "test", method, payload }),
});

test("host only registers the fixed owner-protected route", () => {
  const routes = [];
  apply({ connection: { fetch: { register(value) { routes.push(value); } } } }, config);
  assert.deepEqual(routes.map((r) => r.path), Object.keys(fields).map((key) => `/api/campus-radar/${key}`));
  for (const route of routes) { assert.deepEqual(route.methods, ["POST"]); assert.equal(route.requestBody, "buffered"); }
  assert.throws(() => createHandler({ ...config, python: "python3" }), /absolute/);
});

test("untrusted request cannot choose a command or database path", async () => {
  const handler = createHandler(config);
  assert.equal((await handler(request({ dbPath: "/secret" }))).status, 400);
  assert.equal((await handler(request({}, "exec"))).status, 400);
  const response = await handler(request());
  const data = await response.json();
  assert.equal(data.result.ok, false);
  assert.equal(data.result.error.code, "campus-radar/read-failed");
  assert.deepEqual(data.result.error.details, {});
  assert.equal(response.headers.get("cache-control"), "no-store");
});

test("installed defaults use package-relative code and selected user data", async () => {
  const options = runtimeConfig({ dataDir: "/tmp/campus-radar-uninitialized-test" });
  assert.equal(options.dbPath, "/tmp/campus-radar-uninitialized-test/public.sqlite3");
  assert.equal(options.python, "/tmp/campus-radar-uninitialized-test/venv/bin/python3");
  assert.equal(new URL("../campus_radar/__main__.py", import.meta.url).pathname, new URL("campus_radar/__main__.py", `file://${options.repoRoot}`).pathname);
  const response = await createHandler({ dataDir: "/tmp/campus-radar-uninitialized-test" })(request());
  assert.equal((await response.json()).result.error.code, "campus-radar/setup-required");
  assert.throws(() => runtimeConfig({ dataDir: "relative" }), /absolute/);
});

test("client registers independent page and sidebar entry, then reads through DSH", async () => {
  let entry;
  const react = { createElement() {}, useState() {}, useEffect() {} };
  vm.runInNewContext(await readFile(new URL("client.js", import.meta.url), "utf8"), {
    window: { __ModuleLoader__: { load(value) { entry = value; } } }, URL,
  });
  assert.equal(entry.id, "campus-radar-dsh");
  const plugin = entry.factory((name) => { assert.equal(name, "react"); return react; });
  const registrations = [];
  const calls = [];
  plugin.apply({
    slots: { inject(name, callback) { callback(); }, register(options, component) { registrations.push({ options, component }); } },
    connection: { rpc: { call(...args) { calls.push(args); return Promise.resolve({ ok: true }); } } },
  });
  assert.equal(registrations[0].options.name, "main");
  assert.equal(registrations[0].options.key, "campus-radar");
  assert.equal(registrations[1].options.label, "校园雷达");
  await registrations[0].options.inject().call("list");
  assert.equal(JSON.stringify(calls), JSON.stringify([["/api", "campus-radar/list", {}]]));
});

async function renderPanel(data, error = "", tab = "all") {
  let entry, panel;
  const states = [data, false, error, 0, tab];
  const react = {
    createElement(tag, props, ...children) { return { tag, props, children }; },
    useState(initial) { return [states.length ? states.shift() : initial, () => {}]; }, useEffect() {},
  };
  vm.runInNewContext(await readFile(new URL("client.js", import.meta.url), "utf8"), {
    window: { __ModuleLoader__: { load(value) { entry = value; } } }, URL,
  });
  entry.factory(() => react).apply({
    slots: { inject(name, callback) { callback(); }, register(options, component) { if (options.name === "main") panel = component; } },
    connection: {},
  });
  const text = (node) => typeof node === "string" ? node : node?.children?.map(text).join(" ") ?? "";
  return text(panel({ call() {} }));
}

const dataFor = (event) => ({ sources: [], runs: [], favorites: [], feedback: {}, events: [event], total: 1, next_offset: null });

test("first read failure is not reported as an empty database", async () => {
  const content = await renderPanel(null, "数据库读取失败");
  assert.match(content, /尚未取得数据库结果/);
  assert.doesNotMatch(content, /共 0 条/);
});

test("date-only evidence stays date-only and separate deadlines remain visible", async () => {
  const content = await renderPanel(dataFor({ id: "conference", title: "会议征稿", starts_at: "2027-04-16", ends_at: "2027-04-18",
    registration_deadline: null, audience: "高校教师", eligibility: null, status: "scheduled_registration_unknown", sources: [] }));
  assert.match(content, /2027-04-16（时刻未知） — 2027-04-18（时刻未知）/);
  assert.match(content, /原文受众 高校教师/);
  assert.match(content, /参加资格 未确认/);
  assert.match(content, /截止时间 未确认/);
});

test("favorite and explicit feedback controls reflect persisted state", async () => {
  const data = dataFor({ id: "saved", title: "讲座", status: "unknown", sources: [] });
  data.favorites = ["saved"];
  data.feedback.saved = "known";
  const content = await renderPanel(data);
  assert.match(content, /取消收藏/);
  assert.match(content, /已经知道/);
  assert.match(content, /全部活动（不受偏好隐藏）/);
});

test("reference-only documents keep their provenance", async () => {
  const content = await renderPanel({ ...dataFor({}), documents: [{ id: "ref", title: "官方活动", url: "https://example.edu/lecture", source_name: "官网", processing_status: "manual_reference", events: [], evidence: {} }] }, "", "documents");
  assert.match(content, /非自动采集核对样本/);
  assert.match(content, /识别 0 个活动/);
});

test("digest shows reasons and does not manufacture rows", async () => {
  const content = await renderPanel({ ...dataFor({}), history: [], digest: { day: "2026-10-28", items: [], generated_at: "2026-10-28T01:00:00Z" } }, "", "digest");
  assert.match(content, /0 条/);
  assert.match(content, /当前没有符合规则的新条目/);
});

test("WeRead source exposes update controls and its latest-only coverage", async () => {
  const content = await renderPanel({ sources: [{ id: "wechat-me-liyuan", name: "ME砺远学术", visibility: "public",
    enabled: true, adapter: "weread_latest", coverage: "每次仅检查最新一篇，不能回补历史" }], runs: [] }, "", "sources");
  assert.match(content, /采集已启用/);
  assert.match(content, /更新此来源/);
  assert.match(content, /不能回补历史/);
});

test("successful bounded collection keeps unfinished coverage visible", async () => {
  const content = await renderPanel({ sources: [{ id: "me", name: "官网", visibility: "public", enabled: true, adapter: "sjtu_me" }],
    runs: [{ source_id: "me", status: "success", document_count: 12, inserted_count: 12, updated_count: 0,
      coverage: { collection_pending: true, pending_list_pages: ["https://example.edu/page3"], pending_documents: 2 }, errors: [] }] }, "", "sources");
  assert.match(content, /仍待补扫 1 页／2 篇/);
  assert.match(content, /本轮成功不代表覆盖完成/);
});
