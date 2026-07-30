import {
  existsSync,
  closeSync,
  mkdirSync,
  openSync,
  readFileSync,
  readdirSync,
  renameSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { basename, dirname, join, resolve, sep } from "node:path";
import { randomUUID } from "node:crypto";

const SCHEMA_VERSION = 1;
const STALE_CONSUMPTION_MS = 5 * 60 * 1000;

function identity(cwd, worktreeDir) {
  if (worktreeDir) {
    return { kind: "worktree", value: resolve(worktreeDir) };
  }
  return { kind: "cwd", value: resolve(cwd) };
}

function metadataPathFor(promptPath) {
  if (!promptPath.endsWith("-prompt.md")) {
    throw new Error("Handoff prompt path must end with -prompt.md.");
  }
  return promptPath.slice(0, -3) + "json";
}

function assertPromptUnderRoot(root, promptPath) {
  const resolvedRoot = resolve(root);
  const resolvedPrompt = resolve(promptPath);
  if (!resolvedPrompt.startsWith(`${resolvedRoot}${sep}`)) {
    throw new Error("Handoff path is outside the session-state root.");
  }
  return resolvedPrompt;
}

function atomicWriteJson(path, value) {
  const tmp = join(
    dirname(path),
    `.${basename(path)}.${process.pid}.${randomUUID()}.tmp`,
  );
  writeFileSync(tmp, JSON.stringify(value, null, 2) + "\n", "utf-8");
  renameSync(tmp, path);
}

function readMetadata(path) {
  const metadata = JSON.parse(readFileSync(path, "utf-8"));
  if (metadata.schema_version !== SCHEMA_VERSION) {
    throw new Error(`Unsupported handoff metadata schema: ${metadata.schema_version}`);
  }
  return metadata;
}

function acquireMetadataLock(lockPath, now) {
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      return openSync(lockPath, "wx");
    } catch (error) {
      if (error?.code !== "EEXIST") throw error;
      try {
        const age = now.getTime() - statSync(lockPath).mtimeMs;
        if (age < STALE_CONSUMPTION_MS) {
          throw new Error("Handoff state is currently being updated.");
        }
        unlinkSync(lockPath);
      } catch (lockError) {
        if (lockError?.code === "ENOENT") continue;
        throw lockError;
      }
    }
  }
  throw new Error("Handoff state is currently being updated.");
}

function withMetadataLock(metadataPath, now, callback) {
  const lockPath = `${metadataPath}.lock`;
  const fd = acquireMetadataLock(lockPath, now);
  try {
    return callback();
  } finally {
    try {
      closeSync(fd);
    } finally {
      try {
        unlinkSync(lockPath);
      } catch (error) {
        if (error?.code !== "ENOENT") throw error;
      }
    }
  }
}

function matchesIdentity(metadata, cwd, worktreeDir) {
  const current = identity(cwd, worktreeDir);
  return (
    metadata.worktree_identity?.kind === current.kind &&
    metadata.worktree_identity?.value === current.value
  );
}

function isStaleConsumption(metadata, now) {
  if (metadata.state !== "consuming" || !metadata.consumption_started_at) {
    return false;
  }
  const started = Date.parse(metadata.consumption_started_at);
  return Number.isFinite(started) && now.getTime() - started >= STALE_CONSUMPTION_MS;
}

export function saveHandoffFile({
  root,
  sessionId,
  promptText,
  cwd,
  worktreeDir = null,
  now = new Date(),
}) {
  const filesDir = join(root, sessionId, "files");
  mkdirSync(filesDir, { recursive: true });
  const promptPath = join(filesDir, `${sessionId}-prompt.md`);
  const metadataPath = metadataPathFor(promptPath);
  const timestamp = now.toISOString();
  writeFileSync(promptPath, promptText, "utf-8");
  atomicWriteJson(metadataPath, {
    schema_version: SCHEMA_VERSION,
    session_id: sessionId,
    prompt_path: promptPath,
    cwd: resolve(cwd),
    worktree_id: worktreeDir ? basename(resolve(worktreeDir)) : null,
    worktree_dir: worktreeDir ? resolve(worktreeDir) : null,
    worktree_identity: identity(cwd, worktreeDir),
    state: "pending",
    created_at: timestamp,
    updated_at: timestamp,
  });
  return { promptPath, metadataPath };
}

export function findPendingHandoffFile({
  root,
  cwd,
  worktreeDir = null,
  now = new Date(),
}) {
  if (!existsSync(root)) return null;
  let sessions;
  try {
    sessions = readdirSync(root);
  } catch {
    return null;
  }

  const candidates = [];
  for (const sessionId of sessions) {
    const promptPath = join(root, sessionId, "files", `${sessionId}-prompt.md`);
    const metadataPath = metadataPathFor(promptPath);
    if (!existsSync(promptPath) || !existsSync(metadataPath)) continue;
    try {
      const metadata = readMetadata(metadataPath);
      const selectable =
        metadata.state === "pending" || isStaleConsumption(metadata, now);
      if (!selectable || !matchesIdentity(metadata, cwd, worktreeDir)) continue;
      candidates.push({
        promptPath,
        metadataPath,
        metadata,
        text: readFileSync(promptPath, "utf-8"),
      });
    } catch {
      // Malformed or unreadable handoffs are not safe resume candidates.
    }
  }

  candidates.sort((a, b) =>
    Date.parse(b.metadata.created_at) - Date.parse(a.metadata.created_at),
  );
  return candidates[0] || null;
}

export function beginHandoffFileConsumption({
  root,
  promptPath,
  cwd,
  worktreeDir = null,
  consumerSessionId,
  now = new Date(),
}) {
  promptPath = assertPromptUnderRoot(root, promptPath);
  const metadataPath = metadataPathFor(promptPath);
  if (!existsSync(promptPath) || !existsSync(metadataPath)) {
    throw new Error("Handoff file or metadata does not exist.");
  }
  const token = withMetadataLock(metadataPath, now, () => {
    const metadata = readMetadata(metadataPath);
    if (
      typeof metadata.prompt_path !== "string" ||
      resolve(metadata.prompt_path) !== promptPath
    ) {
      throw new Error("Handoff metadata does not match the prompt path.");
    }
    if (!matchesIdentity(metadata, cwd, worktreeDir)) {
      throw new Error("Handoff belongs to a different worktree.");
    }
    if (metadata.state === "consumed") {
      throw new Error("Handoff was already consumed.");
    }
    if (metadata.state === "consuming" && !isStaleConsumption(metadata, now)) {
      throw new Error("Handoff is currently being consumed.");
    }

    const nextToken = randomUUID();
    const timestamp = now.toISOString();
    atomicWriteJson(metadataPath, {
      ...metadata,
      state: "consuming",
      consumption_token: nextToken,
      consumption_started_at: timestamp,
      consuming_session: consumerSessionId,
      updated_at: timestamp,
    });
    return nextToken;
  });
  return {
    promptPath,
    metadataPath,
    token,
    consumerSessionId,
    text: readFileSync(promptPath, "utf-8"),
  };
}

export function completeHandoffFileConsumption(claim, now = new Date()) {
  withMetadataLock(claim.metadataPath, now, () => {
    const metadata = readMetadata(claim.metadataPath);
    if (
      metadata.state !== "consuming" ||
      metadata.consumption_token !== claim.token
    ) {
      throw new Error("Handoff consumption claim is no longer current.");
    }
    const timestamp = now.toISOString();
    atomicWriteJson(claim.metadataPath, {
      ...metadata,
      state: "consumed",
      consumed_at: timestamp,
      consumed_by_session: claim.consumerSessionId,
      updated_at: timestamp,
      consumption_token: undefined,
      consumption_started_at: undefined,
      consuming_session: undefined,
    });
  });
}

export function abortHandoffFileConsumption(claim, now = new Date()) {
  return withMetadataLock(claim.metadataPath, now, () => {
    const metadata = readMetadata(claim.metadataPath);
    if (
      metadata.state !== "consuming" ||
      metadata.consumption_token !== claim.token
    ) {
      return false;
    }
    atomicWriteJson(claim.metadataPath, {
      ...metadata,
      state: "pending",
      updated_at: now.toISOString(),
      consumption_token: undefined,
      consumption_started_at: undefined,
      consuming_session: undefined,
    });
    return true;
  });
}
