/*
 * zotero-mcp bridge: a Zotero add-on with no UI that lets zotero-mcp, running as a separate program on the
 * same computer, ask the Zotero app to do things only the app can do, such as the item menu's
 * "Find Full Text".
 *
 * It registers two endpoints on Zotero's local HTTP server (127.0.0.1:23119 by default):
 *
 *   GET  /zotero-mcp/ping    -> {ok, id, version, protocol, actions, allowed}
 *   POST /zotero-mcp/action  <- {action, groupID, itemKeys, waitSeconds}
 *
 * Who can call them: Zotero's server binds to 127.0.0.1, refuses Host headers other than localhost
 * (DNS rebinding) and cancels requests that look like they come from a web browser (a Mozilla/ user
 * agent or an Origin header). That holds because these endpoints do not set
 * allowRequestsFromUnsafeWebContent. On top of that, actions run only while Zotero's "Allow other
 * applications on this computer to communicate with Zotero" setting is on, the same consent the local
 * API asks for.
 *
 * Handlers take the Zotero object as a parameter so the tests can run them against a stand-in
 * (tests/zotero_bridge/bootstrap.test.mjs).
 */

"use strict";

var BRIDGE_ID = "bridge@zotero-mcp";
var BRIDGE_PROTOCOL = 1;
var PATH_PREFIX = "/zotero-mcp/";
var MAX_KEYS = 100;
var MAX_WAIT_SECONDS = 600;
var KEY_RE = /^[A-Z0-9]{8}$/;
// Zotero.Attachments.FIND_AVAILABLE_FILE_TYPES, for a Zotero that does not define it.
var FULL_TEXT_TYPES = ["application/pdf", "application/epub+zip"];

var bridgeVersion = "";
var registered = {};

function json(status, body) {
  return [status, "application/json", JSON.stringify(body)];
}

function fail(status, message) {
  return json(status, { ok: false, error: message });
}

function actionsAllowed(Z) {
  return !!Z.Prefs.get("httpServer.localAPI.enabled");
}

/** Why "Find Full Text" would pass over an item, in the words the menu would use if it said. */
function skipReason(Z, item) {
  if (!item.isRegularItem()) {
    if (item.isAttachment && item.isAttachment() && item.parentKey) {
      return "it is an attachment; run Find Full Text on its parent item " + item.parentKey;
    }
    return "not a regular item (it is an attachment, note or annotation)";
  }
  if (item.isFeedItem) return "it is a feed item";
  var types = Z.Attachments.FIND_AVAILABLE_FILE_TYPES || FULL_TEXT_TYPES;
  if (typeof item.numFileAttachmentsWithContentType === "function"
      && types.some(function (t) { return item.numFileAttachmentsWithContentType(t) > 0; })) {
    return "it already has a PDF or EPUB";
  }
  return "it has no DOI, URL or PMCID to search with";
}

/**
 * The item menu's "Find Full Text" (ZoteroPane.findFilesForSelectedItems), for items named by key.
 *
 * Zotero.Attachments.addAvailableFiles does the work, so this shows the same progress window and
 * follows the same rules: per-domain pacing, Retry-After, the user's resolvers and proxy. It resolves
 * when Zotero's whole queue is done; if the user already had a run going, the items join it.
 */
async function findFullText(Z, library, keys, waitSeconds) {
  if (!library.filesEditable) {
    return fail(403, "You cannot add files to this library, so Zotero cannot attach what it finds");
  }
  var A = Z.Attachments;
  var canFind = (A.canFindFileForItem || A.canFindPDFForItem).bind(A);
  var addAll = (A.addAvailableFiles || A.addAvailablePDFs).bind(A);

  var results = [];
  var queued = [];
  for (var key of keys) {
    var item = Z.Items.getByLibraryAndKey(library.libraryID, key);
    if (!item) {
      results.push({ key: key, status: "not_found" });
      continue;
    }
    var row = { key: key, title: item.getDisplayTitle() };
    if (canFind(item)) {
      row.status = "queued";
      queued.push({ row: row, item: item, before: new Set(item.getAttachments()) });
    }
    else {
      row.status = "skipped";
      row.reason = skipReason(Z, item);
    }
    results.push(row);
  }
  // Nothing to search for: answer without calling Zotero, which would pop up "No files found".
  if (!queued.length) {
    return json(200, { ok: true, action: "findFullText", finished: true, results: results });
  }

  var finished = false;
  var failure = null;
  var run = Promise.resolve()
    .then(function () { return addAll(queued.map(function (q) { return q.item; })); })
    .then(
      function () { finished = true; },
      function (e) { finished = true; failure = String(e && e.message || e); Z.logError(e); }
    );
  if (!(waitSeconds > 0)) {
    return json(202, { ok: true, action: "findFullText", finished: false, results: results });
  }
  await Promise.race([run, Z.Promise.delay(waitSeconds * 1000)]);

  for (var q of queued) {
    var added = null;
    for (var id of q.item.getAttachments()) {
      if (q.before.has(id)) continue;
      var att = Z.Items.get(id);
      if (att && att.isFileAttachment && att.isFileAttachment()) { added = att; break; }
    }
    if (added) {
      q.row.status = "found";
      q.row.attachment = {
        key: added.key,
        contentType: added.attachmentContentType || "",
        title: added.getField("title"),
      };
    }
    else if (failure) {
      q.row.status = "failed";
      q.row.reason = failure;
    }
    else {
      q.row.status = finished ? "not_found_online" : "pending";
    }
  }
  return json(200, { ok: true, action: "findFullText", finished: finished, results: results });
}

var ACTIONS = {
  findFullText: findFullText,
};

/** POST /zotero-mcp/action: check the request, find the library, run the named action. */
async function handleAction(Z, data) {
  if (!actionsAllowed(Z)) {
    return fail(403, "Zotero is set not to let other programs on this computer talk to it. Turn on "
      + "\"Allow other applications on this computer to communicate with Zotero\" in Zotero's "
      + "Settings > Advanced, then try again");
  }
  if (!data || typeof data !== "object" || Array.isArray(data)) {
    return fail(400, "Send a JSON object: {action, groupID, itemKeys, waitSeconds}");
  }
  var handler = Object.prototype.hasOwnProperty.call(ACTIONS, data.action) ? ACTIONS[data.action] : null;
  if (!handler) {
    return fail(400, "Unknown action " + JSON.stringify(data.action) + "; this bridge knows "
      + Object.keys(ACTIONS).join(", "));
  }
  var groupID = data.groupID === undefined ? 0 : data.groupID;
  if (!Number.isInteger(groupID) || groupID < 0) {
    return fail(400, "groupID must be 0 (your own library) or the ID of a group");
  }
  var keys = data.itemKeys;
  if (!Array.isArray(keys) || !keys.length) {
    return fail(400, "itemKeys must be a non-empty list of item keys");
  }
  keys = Array.from(new Set(keys));
  if (keys.length > MAX_KEYS) {
    return fail(400, "At most " + MAX_KEYS + " item keys at a time");
  }
  var bad = keys.filter(function (k) { return typeof k !== "string" || !KEY_RE.test(k); });
  if (bad.length) {
    return fail(400, "Not item keys (8 capital letters or digits): " + bad.map(String).join(", "));
  }
  var waitSeconds = data.waitSeconds === undefined ? 0 : data.waitSeconds;
  if (typeof waitSeconds !== "number" || !isFinite(waitSeconds) || waitSeconds < 0
      || waitSeconds > MAX_WAIT_SECONDS) {
    return fail(400, "waitSeconds must be a number from 0 to " + MAX_WAIT_SECONDS);
  }

  var libraryID = groupID === 0 ? Z.Libraries.userLibraryID : Z.Groups.getLibraryIDFromGroupID(groupID);
  var library = libraryID ? Z.Libraries.get(libraryID) : null;
  if (!library) {
    return fail(404, "This Zotero has no group with ID " + groupID);
  }
  if (library.getDataLoaded && !library.getDataLoaded("item")) {
    await library.waitForDataLoad("item");
  }
  return handler(Z, library, keys, waitSeconds);
}

function pingBody(Z) {
  return {
    ok: true,
    id: BRIDGE_ID,
    version: bridgeVersion,
    protocol: BRIDGE_PROTOCOL,
    actions: Object.keys(ACTIONS),
    allowed: actionsAllowed(Z),
  };
}

// Zotero's server calls `new Endpoint()` per request, and passes a one-argument init() a request object
// ({method, pathname, searchParams, headers, data}); the return value is [status, contentType, body].
function PingEndpoint() {}
PingEndpoint.prototype = {
  supportedMethods: ["GET"],
  init: function (request) {
    return json(200, pingBody(Zotero));
  },
};

function ActionEndpoint() {}
ActionEndpoint.prototype = {
  supportedMethods: ["POST"],
  supportedDataTypes: ["application/json"],
  init: async function (request) {
    try {
      return await handleAction(Zotero, request.data);
    }
    catch (e) {
      Zotero.logError(e);
      return fail(500, "The bridge failed: " + (e && e.message || e));
    }
  },
};

var ENDPOINTS = {
  ping: PingEndpoint,
  action: ActionEndpoint,
};

async function startup({ version }) {
  await Zotero.initializationPromise;
  bridgeVersion = version || "";
  for (var name of Object.keys(ENDPOINTS)) {
    var path = PATH_PREFIX + name;
    Zotero.Server.Endpoints[path] = ENDPOINTS[name];
    registered[path] = ENDPOINTS[name];
  }
  Zotero.debug("zotero-mcp bridge " + bridgeVersion + ": listening on " + Object.keys(registered).join(", "));
}

function shutdown() {
  for (var path of Object.keys(registered)) {
    // Only take down what is still ours: a newer copy may have replaced it during an upgrade.
    if (Zotero.Server.Endpoints[path] === registered[path]) delete Zotero.Server.Endpoints[path];
  }
  registered = {};
}

function install() {}
function uninstall() {}
