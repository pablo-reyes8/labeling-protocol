const FOLDER_ID = "13FvCB6Y2Zc4cMY8m213mHRxcDP_jWK8i";
const API_TOKEN = "x94RbYlGtwNLfOiXWCsQzknrTTqY4jFv";
const UPSERT_BY_FILENAME = true;

const CLAIMS_FOLDER_NAME = "__claims";
const CLAIM_TTL_MINUTES = 240;

function doPost(e) {
  try {
    const token = _getParam(e, "token");
    if (API_TOKEN && token !== API_TOKEN) {
      return _text("Unauthorized");
    }

    const action = _getParam(e, "action").toLowerCase();
    if (action === "claim") {
      return _handleClaim(e);
    }

    return _handleSave(e);
  } catch (err) {
    return _text("ERROR: " + err);
  }
}

function _handleClaim(e) {
  if (!e || !e.postData || !e.postData.contents) {
    return _json({ ok: false, error: "body vacio" });
  }

  const body = JSON.parse(e.postData.contents);
  const candidates = Array.isArray(body.candidate_image_ids) ? body.candidate_image_ids : [];
  const requestedCount = Number(body.count || 0);

  if (!requestedCount || requestedCount < 1) {
    return _json({ ok: false, error: "count invalido" });
  }

  const folder = DriveApp.getFolderById(FOLDER_ID);
  const claimed = _claimImageIds(folder, candidates, requestedCount);

  return _json({
    ok: true,
    requested_count: requestedCount,
    claimed_count: claimed.length,
    available_count: claimed.length,
    claimed_image_ids: claimed,
  });
}

function _handleSave(e) {
  if (!e || !e.postData || !e.postData.contents) {
    return _text("ERROR: body vacio");
  }

  const parsed = JSON.parse(e.postData.contents);
  const folder = DriveApp.getFolderById(FOLDER_ID);
  const requestedFilename = _getParam(e, "filename");

  const saved = [];
  if (Array.isArray(parsed)) {
    for (let i = 0; i < parsed.length; i += 1) {
      const item = parsed[i];
      const fileName = _buildFilename(item, requestedFilename, i, parsed.length);
      _writeJsonFile(folder, fileName, item);
      saved.push(fileName);
    }
  } else {
    const fileName = _buildFilename(parsed, requestedFilename, 0, 1);
    _writeJsonFile(folder, fileName, parsed);
    saved.push(fileName);
  }

  return _text("OK: saved " + saved.length + " -> " + saved.join(", "));
}

function _claimImageIds(folder, candidateImageIds, requestedCount) {
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);

  try {
    const candidates = _normalizeCandidateIds(candidateImageIds);
    if (candidates.length === 0) {
      return [];
    }

    const labeledSet = _buildLabeledSet(folder);
    const claimsFolder = _getClaimsFolder(folder, true);
    const activeClaimsSet = _buildActiveClaimsSet(claimsFolder);

    const available = [];
    for (let i = 0; i < candidates.length; i += 1) {
      const imageId = candidates[i];
      if (labeledSet[imageId]) continue;
      if (activeClaimsSet[imageId]) continue;
      available.push(imageId);
    }

    _shuffleInPlace(available);
    const picked = available.slice(0, requestedCount);

    const now = Date.now();
    for (let i = 0; i < picked.length; i += 1) {
      _upsertClaim(claimsFolder, picked[i], now);
    }

    return picked;
  } finally {
    lock.releaseLock();
  }
}

function _normalizeCandidateIds(values) {
  const out = [];
  const seen = {};

  for (let i = 0; i < values.length; i += 1) {
    const raw = String(values[i] || "").trim();
    if (!raw) continue;
    if (seen[raw]) continue;
    seen[raw] = true;
    out.push(raw);
  }

  return out;
}

function _buildLabeledSet(folder) {
  const files = folder.getFiles();
  const set = {};

  while (files.hasNext()) {
    const file = files.next();
    const name = String(file.getName() || "");
    if (!/\.json$/i.test(name)) continue;

    const imageId = name.replace(/\.json$/i, "");
    if (!imageId) continue;
    set[imageId] = true;
  }

  return set;
}

function _buildActiveClaimsSet(claimsFolder) {
  const files = claimsFolder.getFiles();
  const set = {};
  const nowMs = Date.now();
  const ttlMs = CLAIM_TTL_MINUTES * 60 * 1000;

  while (files.hasNext()) {
    const file = files.next();
    const content = String(file.getBlob().getDataAsString() || "");

    try {
      const payload = JSON.parse(content);
      const imageId = String(payload.image_id || "").trim();
      const claimedAtMs = Number(payload.claimed_at_ms || 0);
      if (!imageId) {
        continue;
      }

      if (claimedAtMs > 0 && nowMs - claimedAtMs > ttlMs) {
        file.setTrashed(true);
        continue;
      }

      set[imageId] = true;
    } catch (_err) {
      file.setTrashed(true);
    }
  }

  return set;
}

function _upsertClaim(claimsFolder, imageId, claimedAtMs) {
  const fileName = _claimFileName(imageId);
  const existing = claimsFolder.getFilesByName(fileName);
  while (existing.hasNext()) {
    existing.next().setTrashed(true);
  }

  const payload = {
    image_id: imageId,
    claimed_at_ms: claimedAtMs,
  };
  claimsFolder.createFile(fileName, JSON.stringify(payload), MimeType.PLAIN_TEXT);
}

function _claimFileName(imageId) {
  const encoded = Utilities.base64EncodeWebSafe(String(imageId)).replace(/=+$/g, "");
  return "claim_" + encoded + ".json";
}

function _buildFilename(payload, requestedFilename, index, totalCount) {
  let candidate = String(requestedFilename || "").trim();
  if (!candidate) {
    candidate = _inferFilenameFromPayload(payload);
  }
  if (!candidate) {
    const ts = Utilities.formatDate(new Date(), "UTC", "yyyyMMdd_HHmmss_SSS");
    candidate = "annotation_" + ts + ".json";
  }

  candidate = _sanitizeFilename(candidate);
  if (!candidate.toLowerCase().endsWith(".json")) {
    candidate = candidate + ".json";
  }

  if (totalCount > 1) {
    const suffix = "_" + String(index + 1).padStart(2, "0");
    candidate = candidate.replace(/\.json$/i, "") + suffix + ".json";
  }
  return candidate;
}

function _inferFilenameFromPayload(payload) {
  if (!payload || typeof payload !== "object") {
    return "";
  }

  if (payload.image_id) {
    return String(payload.image_id) + ".json";
  }

  if (payload.image_meta && payload.image_meta.filename) {
    return String(payload.image_meta.filename) + ".json";
  }

  return "";
}

function _inferImageId(payload, fileName) {
  if (payload && typeof payload === "object") {
    if (payload.image_id) {
      return String(payload.image_id).trim();
    }
    if (payload.image_meta && payload.image_meta.filename) {
      return String(payload.image_meta.filename).trim();
    }
  }

  const name = String(fileName || "").trim();
  if (!name) return "";
  return name.replace(/\.json$/i, "");
}

function _sanitizeFilename(name) {
  return String(name).replace(/[\\/:*?"<>|]/g, "_").trim();
}

function _writeJsonFile(folder, fileName, obj) {
  if (UPSERT_BY_FILENAME) {
    const existing = folder.getFilesByName(fileName);
    while (existing.hasNext()) {
      existing.next().setTrashed(true);
    }
  }

  const content = JSON.stringify(obj);
  folder.createFile(fileName, content, MimeType.PLAIN_TEXT);

  const imageId = _inferImageId(obj, fileName);
  if (imageId) {
    _clearClaim(folder, imageId);
  }
}

function _clearClaim(folder, imageId) {
  const claimsFolder = _getClaimsFolder(folder, false);
  if (!claimsFolder) {
    return;
  }

  const claimName = _claimFileName(imageId);
  const files = claimsFolder.getFilesByName(claimName);
  while (files.hasNext()) {
    files.next().setTrashed(true);
  }
}

function _getClaimsFolder(folder, createIfMissing) {
  const shouldCreate = createIfMissing !== false;
  const folders = folder.getFoldersByName(CLAIMS_FOLDER_NAME);
  if (folders.hasNext()) {
    return folders.next();
  }
  if (!shouldCreate) {
    return null;
  }
  return folder.createFolder(CLAIMS_FOLDER_NAME);
}

function _shuffleInPlace(arr) {
  for (let i = arr.length - 1; i > 0; i -= 1) {
    const j = Math.floor(Math.random() * (i + 1));
    const temp = arr[i];
    arr[i] = arr[j];
    arr[j] = temp;
  }
}

function _getParam(e, key) {
  if (!e || !e.parameter || !e.parameter[key]) {
    return "";
  }
  return String(e.parameter[key]);
}

function _text(message) {
  return ContentService.createTextOutput(message).setMimeType(ContentService.MimeType.TEXT);
}

function _json(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
