// Tests for the zotero-mcp bridge add-on (src/zotero_mcp/zotero_bridge/bootstrap.js).
//
// bootstrap.js is loaded into a fresh vm context with a stand-in for Zotero, the way Zotero loads it into
// a plugin scope, and its endpoints are driven the way Zotero's server drives them: `new Endpoint()`, then
// a one-argument init(request) whose result is [status, contentType, body].
//
// Run: node --test tests/zotero_bridge/bootstrap.test.mjs   (tests/test_zotero_bridge_js.py runs this from pytest)

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const SOURCE = readFileSync(new URL("../../src/zotero_mcp/zotero_bridge/bootstrap.js", import.meta.url), "utf8");

let nextID = 100;

function fakeItem({ key, title = `Title ${key}`, regular = true, attachment = false, parentKey, eligible = true,
                    hasPDF = false, feed = false, contentType = "" }) {
  return {
    id: nextID++,
    key,
    parentKey,
    isFeedItem: feed,
    attachmentContentType: contentType,
    attachments: [],
    eligible,
    isRegularItem: () => regular,
    isAttachment: () => attachment,
    isFileAttachment: () => attachment,
    getDisplayTitle: () => title,
    getField: (f) => (f === "title" ? title : ""),
    getAttachments() { return this.attachments.slice(); },
    numFileAttachmentsWithContentType: (t) => (hasPDF && t === "application/pdf" ? 1 : 0),
  };
}

function fakeZotero({ items = [], allowed = true, filesEditable = true, groups = { 42: 7 }, attachments } = {}) {
  const byID = new Map(items.map((i) => [i.id, i]));
  const libraries = new Map([[1, { libraryID: 1, filesEditable }], [7, { libraryID: 7, filesEditable }]]);
  const Z = {
    calls: { addAvailableFiles: [], logError: [] },
    initializationPromise: Promise.resolve(),
    Prefs: { get: (name) => (name === "httpServer.localAPI.enabled" ? allowed : undefined) },
    Server: { Endpoints: {} },
    Libraries: { userLibraryID: 1, get: (id) => libraries.get(id) || false },
    Groups: { getLibraryIDFromGroupID: (gid) => groups[gid] || false },
    Items: {
      getByLibraryAndKey: (libraryID, key) => items.find((i) => i.key === key && (i.libraryID ?? 1) === libraryID) || false,
      get: (id) => byID.get(id) || false,
    },
    Attachments: attachments || {
      FIND_AVAILABLE_FILE_TYPES: ["application/pdf", "application/epub+zip"],
      canFindFileForItem: (item) => item.eligible && item.isRegularItem(),
      addAvailableFiles: async (list) => { Z.calls.addAvailableFiles.push(Array.from(list, (i) => i.key)); },
    },
    Promise: { delay: (ms) => new Promise((r) => setTimeout(r, ms)) },
    debug: () => {},
    logError: (e) => Z.calls.logError.push(e),
  };
  Z.attach = (parent, contentType = "application/pdf") => {
    const att = fakeItem({ key: "ATT" + String(nextID).padStart(5, "0"), attachment: true, regular: false,
                           parentKey: parent.key, contentType, title: "Full Text PDF" });
    byID.set(att.id, att);
    parent.attachments.push(att.id);
    return att;
  };
  return Z;
}

async function load(Z, version = "1.2.3") {
  const scope = vm.createContext({ Zotero: Z });
  vm.runInContext(SOURCE, scope, { filename: "bootstrap.js" });
  await scope.startup({ id: "bridge@zotero-mcp", version, rootURI: "jar:file:///x.xpi!/" });
  return scope;
}

async function call(Z, path, data) {
  const Endpoint = Z.Server.Endpoints[path];
  assert.ok(Endpoint, `${path} is registered`);
  const endpoint = new Endpoint();
  assert.equal(endpoint.init.length, 1, "Zotero passes a single request object only to one-parameter init()");
  const [status, type, body] = await endpoint.init({ method: data ? "POST" : "GET", data, headers: {} });
  assert.equal(type, "application/json");
  return { status, body: JSON.parse(body) };
}

const action = (Z, data) => call(Z, "/zotero-mcp/action", data);

test("startup registers the endpoints and shutdown removes them", async () => {
  const Z = fakeZotero();
  const scope = await load(Z);
  assert.deepEqual(Object.keys(Z.Server.Endpoints).sort(), ["/zotero-mcp/action", "/zotero-mcp/ping"]);
  for (const E of Object.values(Z.Server.Endpoints)) {
    assert.equal(E.prototype.allowRequestsFromUnsafeWebContent, undefined, "browser requests stay blocked");
  }
  assert.deepEqual([...Z.Server.Endpoints["/zotero-mcp/ping"].prototype.supportedMethods], ["GET"]);
  assert.deepEqual([...Z.Server.Endpoints["/zotero-mcp/action"].prototype.supportedMethods], ["POST"]);
  scope.shutdown();
  assert.deepEqual(Object.keys(Z.Server.Endpoints), []);
});

test("shutdown leaves an endpoint that another copy has replaced", async () => {
  const Z = fakeZotero();
  const scope = await load(Z);
  const replacement = function () {};
  Z.Server.Endpoints["/zotero-mcp/ping"] = replacement;
  scope.shutdown();
  assert.equal(Z.Server.Endpoints["/zotero-mcp/ping"], replacement);
  assert.equal(Z.Server.Endpoints["/zotero-mcp/action"], undefined);
});

test("ping says what is installed", async () => {
  const Z = fakeZotero({ allowed: false });
  await load(Z, "1.2.3");
  const { status, body } = await call(Z, "/zotero-mcp/ping");
  assert.equal(status, 200);
  assert.deepEqual(body, { ok: true, id: "bridge@zotero-mcp", version: "1.2.3", protocol: 1,
                           actions: ["findFullText"], allowed: false });
});

test("actions are refused while Zotero does not allow other applications", async () => {
  const Z = fakeZotero({ allowed: false, items: [fakeItem({ key: "AAAAAAAA" })] });
  await load(Z);
  const { status, body } = await action(Z, { action: "findFullText", itemKeys: ["AAAAAAAA"] });
  assert.equal(status, 403);
  assert.match(body.error, /Allow other applications/);
  assert.deepEqual(Z.calls.addAvailableFiles, []);
});

test("bad requests get a 400 that says what is wrong", async () => {
  const Z = fakeZotero();
  await load(Z);
  const cases = [
    [null, /JSON object/],
    [["findFullText"], /JSON object/],
    [{ action: "deleteEverything", itemKeys: ["AAAAAAAA"] }, /Unknown action "deleteEverything"/],
    [{ action: "toString", itemKeys: ["AAAAAAAA"] }, /Unknown action/],
    [{ action: "findFullText" }, /non-empty list/],
    [{ action: "findFullText", itemKeys: [] }, /non-empty list/],
    [{ action: "findFullText", itemKeys: ["aaaaaaaa", "AAAAAAAA", 5] }, /Not item keys.*aaaaaaaa, 5/],
    [{ action: "findFullText", itemKeys: Array.from({ length: 101 }, (_, i) => `K${String(i).padStart(7, "0")}`) }, /At most 100/],
    [{ action: "findFullText", itemKeys: ["AAAAAAAA"], groupID: -1 }, /groupID/],
    [{ action: "findFullText", itemKeys: ["AAAAAAAA"], groupID: "42" }, /groupID/],
    [{ action: "findFullText", itemKeys: ["AAAAAAAA"], waitSeconds: 601 }, /waitSeconds/],
    [{ action: "findFullText", itemKeys: ["AAAAAAAA"], waitSeconds: "5" }, /waitSeconds/],
  ];
  for (const [data, message] of cases) {
    const { status, body } = await action(Z, data);
    assert.equal(status, 400, JSON.stringify(data));
    assert.equal(body.ok, false);
    assert.match(body.error, message);
  }
  assert.deepEqual(Z.calls.addAvailableFiles, []);
});

test("an unknown group is a 404, a known one is searched in its own library", async () => {
  const item = fakeItem({ key: "GROUPKEY" });
  item.libraryID = 7;
  const Z = fakeZotero({ items: [item] });
  await load(Z);
  let r = await action(Z, { action: "findFullText", groupID: 99, itemKeys: ["GROUPKEY"] });
  assert.equal(r.status, 404);
  assert.match(r.body.error, /no group with ID 99/);
  r = await action(Z, { action: "findFullText", groupID: 0, itemKeys: ["GROUPKEY"] });
  assert.equal(r.body.results[0].status, "not_found", "the key is not in the personal library");
  r = await action(Z, { action: "findFullText", groupID: 42, itemKeys: ["GROUPKEY"] });
  assert.equal(r.status, 202);
  assert.deepEqual(Z.calls.addAvailableFiles, [["GROUPKEY"]]);
});

test("a library whose files are read-only is refused, as the menu disables the command there", async () => {
  const Z = fakeZotero({ filesEditable: false, items: [fakeItem({ key: "AAAAAAAA" })] });
  await load(Z);
  const { status, body } = await action(Z, { action: "findFullText", itemKeys: ["AAAAAAAA"] });
  assert.equal(status, 403);
  assert.match(body.error, /cannot add files/);
});

test("items Zotero would pass over are reported with the reason, and Zotero is not called", async () => {
  const items = [
    fakeItem({ key: "ATTACHMT", regular: false, attachment: true, parentKey: "PARENT01", eligible: false }),
    fakeItem({ key: "NOTEITEM", regular: false, eligible: false }),
    fakeItem({ key: "HASPDF00", hasPDF: true, eligible: false }),
    fakeItem({ key: "FEEDITEM", feed: true, eligible: false }),
    fakeItem({ key: "NODOI000", eligible: false }),
  ];
  const Z = fakeZotero({ items });
  await load(Z);
  const { status, body } = await action(Z, {
    action: "findFullText", itemKeys: ["ATTACHMT", "NOTEITEM", "HASPDF00", "FEEDITEM", "NODOI000", "MISSING0"], waitSeconds: 5,
  });
  assert.equal(status, 200);
  assert.equal(body.finished, true);
  const reasons = Object.fromEntries(body.results.map((r) => [r.key, r.reason || r.status]));
  assert.match(reasons.ATTACHMT, /attachment; run Find Full Text on its parent item PARENT01/);
  assert.match(reasons.NOTEITEM, /not a regular item/);
  assert.match(reasons.HASPDF00, /already has a PDF or EPUB/);
  assert.match(reasons.FEEDITEM, /feed item/);
  assert.match(reasons.NODOI000, /no DOI, URL or PMCID/);
  assert.equal(reasons.MISSING0, "not_found");
  assert.deepEqual(Z.calls.addAvailableFiles, [], "no 'No files found' popup for nothing");
});

test("waiting reports which items got a file", async () => {
  const hit = fakeItem({ key: "HIT00000", title: "Has an OA copy" });
  const miss = fakeItem({ key: "MISS0000" });
  const Z = fakeZotero({ items: [hit, miss] });
  Z.attach(hit, "text/html"); // a snapshot from before does not count as found
  hit.attachments.length = 0;
  let added;
  Z.Attachments.addAvailableFiles = async (list) => {
    Z.calls.addAvailableFiles.push(Array.from(list, (i) => i.key));
    added = Z.attach(hit, "application/pdf");
  };
  await load(Z);
  const { status, body } = await action(Z, { action: "findFullText", itemKeys: ["HIT00000", "MISS0000", "HIT00000"], waitSeconds: 5 });
  assert.equal(status, 200);
  assert.equal(body.finished, true);
  assert.deepEqual(Z.calls.addAvailableFiles, [["HIT00000", "MISS0000"]], "keys are deduplicated");
  assert.deepEqual(body.results, [
    { key: "HIT00000", title: "Has an OA copy", status: "found",
      attachment: { key: added.key, contentType: "application/pdf", title: "Full Text PDF" } },
    { key: "MISS0000", title: "Title MISS0000", status: "not_found_online" },
  ]);
});

test("attachments the item already had are not reported as found", async () => {
  const item = fakeItem({ key: "OLDATTCH" });
  const Z = fakeZotero({ items: [item] });
  Z.attach(item, "text/html");
  await load(Z);
  const { body } = await action(Z, { action: "findFullText", itemKeys: ["OLDATTCH"], waitSeconds: 5 });
  assert.equal(body.results[0].status, "not_found_online");
});

test("waitSeconds 0 queues and answers 202 at once", async () => {
  const Z = fakeZotero({ items: [fakeItem({ key: "AAAAAAAA" })] });
  let release;
  Z.Attachments.addAvailableFiles = (list) => {
    Z.calls.addAvailableFiles.push(Array.from(list, (i) => i.key));
    return new Promise((r) => { release = r; });
  };
  await load(Z);
  const { status, body } = await action(Z, { action: "findFullText", itemKeys: ["AAAAAAAA"], waitSeconds: 0 });
  assert.equal(status, 202);
  assert.equal(body.finished, false);
  assert.equal(body.results[0].status, "queued");
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(Z.calls.addAvailableFiles, [["AAAAAAAA"]]);
  release();
});

test("when the wait runs out, unfinished items are pending", async () => {
  const Z = fakeZotero({ items: [fakeItem({ key: "SLOW0000" })] });
  Z.Attachments.addAvailableFiles = () => new Promise(() => {});
  await load(Z);
  const { status, body } = await action(Z, { action: "findFullText", itemKeys: ["SLOW0000"], waitSeconds: 0.05 });
  assert.equal(status, 200);
  assert.equal(body.finished, false);
  assert.equal(body.results[0].status, "pending");
});

test("a failure inside Zotero is reported per item and logged", async () => {
  const Z = fakeZotero({ items: [fakeItem({ key: "AAAAAAAA" })] });
  Z.Attachments.addAvailableFiles = async () => { throw new Error("network down"); };
  await load(Z);
  const { body } = await action(Z, { action: "findFullText", itemKeys: ["AAAAAAAA"], waitSeconds: 5 });
  assert.equal(body.finished, true);
  assert.deepEqual(body.results[0], { key: "AAAAAAAA", title: "Title AAAAAAAA", status: "failed", reason: "network down" });
  assert.equal(Z.calls.logError.length, 1);
});

test("Zotero 7's older names for the same functions work too", async () => {
  const item = fakeItem({ key: "AAAAAAAA" });
  const calls = [];
  const Z = fakeZotero({
    items: [item],
    attachments: {
      canFindPDFForItem: () => true,
      addAvailablePDFs: async (list) => { calls.push(Array.from(list, (i) => i.key)); },
    },
  });
  await load(Z);
  const { body } = await action(Z, { action: "findFullText", itemKeys: ["AAAAAAAA"], waitSeconds: 5 });
  assert.deepEqual(calls, [["AAAAAAAA"]]);
  assert.equal(body.results[0].status, "not_found_online");
});

test("an unexpected exception becomes a 500, not a hung request", async () => {
  const Z = fakeZotero({ items: [fakeItem({ key: "AAAAAAAA" })] });
  Z.Items.getByLibraryAndKey = () => { throw new Error("database is locked"); };
  await load(Z);
  const { status, body } = await action(Z, { action: "findFullText", itemKeys: ["AAAAAAAA"] });
  assert.equal(status, 500);
  assert.match(body.error, /database is locked/);
  assert.equal(Z.calls.logError.length, 1);
});
