const FOLDER_ID = "13FvCB6Y2Zc4cMY8m213mHRxcDP_jWK8i";
const SOURCE_IMAGES_FOLDER_ID = "1LN7VNGPRz6kZN5a_VvbOCkbMXG6y7F3u";
const API_TOKEN = "x94RbYlGtwNLfOiXWCsQzknrTTqY4jFv";
const UPSERT_BY_FILENAME = true;

const CLAIMS_FOLDER_NAME = "__claims";
const ACTIVE_CLAIMS_FOLDER_NAME = "__active_claims";
const CLAIM_TTL_MINUTES = 240;
const IMAGE_NAME_REGEX = /\.(jpg|jpeg|png|webp|bmp|tif|tiff)$/i;
const SOURCE_MANIFEST_FILE_PREFIX = "__source_manifest__";

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
    if (action === "claim_remote") {
      return _handleClaimRemote(e);
    }
    if (action === "download_image") {
      return _handleDownloadImage(e);
    }
    if (action === "rebuild_source_manifest") {
      return _handleRebuildSourceManifest(e);
    }

    return _handleSave(e);
  } catch (err) {
    return _text("ERROR: " + err);
  }
}

function buildSourceManifestNow() {
  const sourceFolderId = String(SOURCE_IMAGES_FOLDER_ID || "").trim();
  if (!sourceFolderId) {
    throw new Error("SOURCE_IMAGES_FOLDER_ID vacio");
  }

  const annotationsFolder = DriveApp.getFolderById(FOLDER_ID);
  const sourceFolder = DriveApp.getFolderById(sourceFolderId);
  const images = _buildSourceManifestEntries(sourceFolder);
  _writeSourceManifest(annotationsFolder, sourceFolderId, images, Date.now());

  return {
    ok: true,
    source_folder_id: sourceFolderId,
    image_count: images.length,
  };
}

function rebuildSourceManifestNow() {
  return buildSourceManifestNow();
}

function _handleRebuildSourceManifest(e) {
  const body = e && e.postData && e.postData.contents ? JSON.parse(e.postData.contents) : {};
  const sourceFolderId = String(body.source_folder_id || SOURCE_IMAGES_FOLDER_ID || "").trim();
  if (!sourceFolderId) {
    return _json({ ok: false, error: "source_folder_id vacio" });
  }

  const annotationsFolder = DriveApp.getFolderById(FOLDER_ID);
  const sourceFolder = DriveApp.getFolderById(sourceFolderId);
  const images = _buildSourceManifestEntries(sourceFolder);
  _writeSourceManifest(annotationsFolder, sourceFolderId, images, Date.now());

  return _json({
    ok: true,
    source_folder_id: sourceFolderId,
    image_count: images.length,
    rebuilt: true,
  });
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

function _handleClaimRemote(e) {
  if (!e || !e.postData || !e.postData.contents) {
    return _json({ ok: false, error: "body vacio" });
  }

  const body = JSON.parse(e.postData.contents);
  const requestedCount = Number(body.count || 0);
  const sourceFolderId = String(body.source_folder_id || SOURCE_IMAGES_FOLDER_ID || "").trim();

  if (!requestedCount || requestedCount < 1) {
    return _json({ ok: false, error: "count invalido" });
  }
  if (!sourceFolderId) {
    return _json({ ok: false, error: "source_folder_id vacio" });
  }

  const annotationsFolder = DriveApp.getFolderById(FOLDER_ID);
  const sourceFolder = DriveApp.getFolderById(sourceFolderId);
  const sourceManifest = _getSourceManifestEntries(annotationsFolder, sourceFolder, sourceFolderId);
  const claimResult = _claimRemoteImages(annotationsFolder, sourceManifest, requestedCount);

  return _json({
    ok: true,
    requested_count: requestedCount,
    claimed_count: claimResult.claimed_images.length,
    available_count: claimResult.available_count,
    claimed_images: claimResult.claimed_images,
  });
}

function _handleDownloadImage(e) {
  if (!e || !e.postData || !e.postData.contents) {
    return _json({ ok: false, error: "body vacio" });
  }

  const body = JSON.parse(e.postData.contents);
  const fileId = String(body.file_id || "").trim();
  const sourceFolderId = String(body.source_folder_id || SOURCE_IMAGES_FOLDER_ID || "").trim();

  if (!fileId) {
    return _json({ ok: false, error: "file_id vacio" });
  }
  if (!sourceFolderId) {
    return _json({ ok: false, error: "source_folder_id vacio" });
  }

  const file = DriveApp.getFileById(fileId);
  if (!_fileBelongsToFolder(file, sourceFolderId)) {
    return _json({ ok: false, error: "archivo fuera de la carpeta fuente" });
  }

  const blob = file.getBlob();
  const bytes = blob.getBytes();

  return _json({
    ok: true,
    file_id: file.getId(),
    image_id: file.getName(),
    name: file.getName(),
    mime_type: file.getMimeType(),
    size_bytes: Number(file.getSize() || 0),
    resource_key:
      typeof file.getResourceKey === "function" ? String(file.getResourceKey() || "") : "",
    base64_data: Utilities.base64Encode(bytes),
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
    const activeClaimsFolder = _getClaimsFolder(folder, ACTIVE_CLAIMS_FOLDER_NAME, true);
    const activeClaimsSet = _buildActiveClaimsSet(activeClaimsFolder);

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
      _upsertActiveClaim(activeClaimsFolder, picked[i], now);
    }

    return picked;
  } finally {
    lock.releaseLock();
  }
}

function _claimRemoteImages(annotationsFolder, sourceImages, requestedCount) {
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);

  try {
    const labeledSet = _buildLabeledSet(annotationsFolder);
    const activeClaimsFolder = _getClaimsFolder(
      annotationsFolder,
      ACTIVE_CLAIMS_FOLDER_NAME,
      true
    );
    const activeClaimsSet = _buildActiveClaimsSet(activeClaimsFolder);
    const sampleResult = _sampleAvailableRemoteImages(
      sourceImages,
      labeledSet,
      activeClaimsSet,
      requestedCount
    );
    const picked = sampleResult.sampled_images;

    const now = Date.now();
    for (let i = 0; i < picked.length; i += 1) {
      _upsertActiveClaim(activeClaimsFolder, picked[i].image_id, now);
    }

    return {
      claimed_images: picked,
      available_count: sampleResult.available_count,
    };
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

function _sampleAvailableRemoteImages(sourceImages, labeledSet, activeClaimsSet, requestedCount) {
  const sampled = [];
  let availableCount = 0;

  for (let i = 0; i < sourceImages.length; i += 1) {
    const imageInfo = sourceImages[i];
    const imageId = String(imageInfo.image_id || "").trim();
    if (!imageId) {
      continue;
    }
    if (labeledSet[imageId]) {
      continue;
    }
    if (activeClaimsSet[imageId]) {
      continue;
    }

    availableCount += 1;
    if (sampled.length < requestedCount) {
      sampled.push(imageInfo);
      continue;
    }

    const replaceIndex = Math.floor(Math.random() * availableCount);
    if (replaceIndex < requestedCount) {
      sampled[replaceIndex] = imageInfo;
    }
  }

  return {
    sampled_images: sampled,
    available_count: availableCount,
  };
}

function _getSourceManifestEntries(annotationsFolder, sourceFolder, sourceFolderId) {
  const manifestFile = _getSourceManifestFile(annotationsFolder, sourceFolderId);

  if (manifestFile) {
    try {
      const payload = JSON.parse(String(manifestFile.getBlob().getDataAsString() || ""));
      const manifestFolderId = String(payload.source_folder_id || "").trim();
      const images = Array.isArray(payload.images) ? payload.images : [];
      if (manifestFolderId === sourceFolderId && images.length > 0) {
        return images;
      }
    } catch (_err) {
      // Rebuild below.
    }
  }

  const rebuiltImages = _buildSourceManifestEntries(sourceFolder);
  _writeSourceManifest(annotationsFolder, sourceFolderId, rebuiltImages, Date.now());
  return rebuiltImages;
}

function _buildSourceManifestEntries(sourceFolder) {
  const files = sourceFolder.getFiles();
  const images = [];

  while (files.hasNext()) {
    const file = files.next();
    if (!_isSupportedImageFile(file)) {
      continue;
    }

    const imageId = String(file.getName() || "").trim();
    if (!imageId) {
      continue;
    }

    images.push({
      image_id: imageId,
      file_id: file.getId(),
      name: imageId,
      size_bytes: Number(file.getSize() || 0),
      mime_type: String(file.getMimeType() || ""),
      resource_key:
        typeof file.getResourceKey === "function" ? String(file.getResourceKey() || "") : "",
    });
  }

  return images;
}

function _writeSourceManifest(annotationsFolder, sourceFolderId, images, generatedAtMs) {
  const manifestName = _sourceManifestFileName(sourceFolderId);
  const existing = annotationsFolder.getFilesByName(manifestName);
  while (existing.hasNext()) {
    existing.next().setTrashed(true);
  }

  const payload = {
    source_folder_id: sourceFolderId,
    generated_at_ms: generatedAtMs,
    image_count: images.length,
    images: images,
  };
  annotationsFolder.createFile(manifestName, JSON.stringify(payload), MimeType.PLAIN_TEXT);
}

function _getSourceManifestFile(annotationsFolder, sourceFolderId) {
  const manifestName = _sourceManifestFileName(sourceFolderId);
  const files = annotationsFolder.getFilesByName(manifestName);
  if (files.hasNext()) {
    return files.next();
  }
  return null;
}

function _sourceManifestFileName(sourceFolderId) {
  return SOURCE_MANIFEST_FILE_PREFIX + sourceFolderId + ".json";
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

function _upsertActiveClaim(claimsFolder, imageId, claimedAtMs) {
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

function _isSupportedImageFile(file) {
  const name = String(file.getName() || "");
  return IMAGE_NAME_REGEX.test(name);
}

function _fileBelongsToFolder(file, folderId) {
  const parents = file.getParents();
  const targetId = String(folderId || "").trim();
  if (!targetId) {
    return false;
  }

  while (parents.hasNext()) {
    const parent = parents.next();
    if (String(parent.getId() || "") === targetId) {
      return true;
    }
  }

  return false;
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
    _upsertCompletedClaim(folder, imageId, Date.now());
    _clearActiveClaim(folder, imageId);
  }
}

function _clearActiveClaim(folder, imageId) {
  const claimsFolder = _getClaimsFolder(folder, ACTIVE_CLAIMS_FOLDER_NAME, false);
  if (!claimsFolder) {
    return;
  }

  const claimName = _claimFileName(imageId);
  const files = claimsFolder.getFilesByName(claimName);
  while (files.hasNext()) {
    files.next().setTrashed(true);
  }
}

function _upsertCompletedClaim(folder, imageId, completedAtMs) {
  const claimsFolder = _getClaimsFolder(folder, CLAIMS_FOLDER_NAME, true);
  const fileName = _claimFileName(imageId);
  const existing = claimsFolder.getFilesByName(fileName);
  while (existing.hasNext()) {
    existing.next().setTrashed(true);
  }

  const payload = {
    image_id: imageId,
    completed_at_ms: completedAtMs,
  };
  claimsFolder.createFile(fileName, JSON.stringify(payload), MimeType.PLAIN_TEXT);
}

function _getClaimsFolder(folder, folderName, createIfMissing) {
  const shouldCreate = createIfMissing !== false;
  const targetName = String(folderName || "").trim();
  if (!targetName) {
    return null;
  }

  const folders = folder.getFoldersByName(targetName);
  if (folders.hasNext()) {
    return folders.next();
  }
  if (!shouldCreate) {
    return null;
  }
  return folder.createFolder(targetName);
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
