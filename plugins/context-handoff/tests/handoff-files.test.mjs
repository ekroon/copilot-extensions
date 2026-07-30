import assert from "node:assert/strict";
import {
  mkdtempSync,
  mkdirSync,
  readFileSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  abortHandoffFileConsumption,
  beginHandoffFileConsumption,
  completeHandoffFileConsumption,
  findPendingHandoffFile,
  saveHandoffFile,
} from "../extensions/context-handoff/handoff-files.mjs";

function fixture() {
  const root = mkdtempSync(join(tmpdir(), "handoff-files-"));
  mkdirSync(root, { recursive: true });
  return root;
}

test("selects only the newest pending handoff for the exact worktree", () => {
  const root = fixture();
  saveHandoffFile({
    root,
    sessionId: "matching-old",
    promptText: "matching old",
    cwd: "/repo/worktree",
    worktreeDir: "/repo/worktree",
    now: new Date("2026-07-30T08:00:00Z"),
  });
  saveHandoffFile({
    root,
    sessionId: "foreign-new",
    promptText: "foreign new",
    cwd: "/repo/other",
    worktreeDir: "/repo/other",
    now: new Date("2026-07-30T10:00:00Z"),
  });
  const matching = saveHandoffFile({
    root,
    sessionId: "matching-new",
    promptText: "matching new",
    cwd: "/repo/worktree/subdir",
    worktreeDir: "/repo/worktree",
    now: new Date("2026-07-30T09:00:00Z"),
  });

  const found = findPendingHandoffFile({
    root,
    cwd: "/repo/worktree/another-subdir",
    worktreeDir: "/repo/worktree",
  });

  assert.equal(found?.promptPath, matching.promptPath);
  assert.equal(found?.text, "matching new");
});

test("does not guess from a legacy prose-only handoff", () => {
  const root = fixture();
  const filesDir = join(root, "legacy", "files");
  mkdirSync(filesDir, { recursive: true });
  writeFileSync(
    join(filesDir, "legacy-prompt.md"),
    "**CWD:** /repo/worktree\n\nContinue this work.",
    "utf-8",
  );

  assert.equal(
    findPendingHandoffFile({
      root,
      cwd: "/repo/worktree",
      worktreeDir: "/repo/worktree",
    }),
    null,
  );
});

test("marks a claimed handoff consumed exactly once", () => {
  const root = fixture();
  const saved = saveHandoffFile({
    root,
    sessionId: "source",
    promptText: "continue",
    cwd: "/repo/worktree",
    worktreeDir: "/repo/worktree",
  });
  const claim = beginHandoffFileConsumption({
    root,
    promptPath: saved.promptPath,
    cwd: "/repo/worktree",
    worktreeDir: "/repo/worktree",
    consumerSessionId: "successor",
  });

  completeHandoffFileConsumption(claim);

  assert.equal(
    findPendingHandoffFile({
      root,
      cwd: "/repo/worktree",
      worktreeDir: "/repo/worktree",
    }),
    null,
  );
  assert.throws(
    () =>
      beginHandoffFileConsumption({
        root,
        promptPath: saved.promptPath,
        cwd: "/repo/worktree",
        worktreeDir: "/repo/worktree",
        consumerSessionId: "another",
      }),
    /already consumed/,
  );
  const metadata = JSON.parse(readFileSync(saved.metadataPath, "utf-8"));
  assert.equal(metadata.state, "consumed");
  assert.equal(metadata.consumed_by_session, "successor");
});

test("returns a failed load to pending so it remains recoverable", () => {
  const root = fixture();
  const saved = saveHandoffFile({
    root,
    sessionId: "source",
    promptText: "continue",
    cwd: "/repo/worktree",
    worktreeDir: "/repo/worktree",
  });
  const claim = beginHandoffFileConsumption({
    root,
    promptPath: saved.promptPath,
    cwd: "/repo/worktree",
    worktreeDir: "/repo/worktree",
    consumerSessionId: "failed-successor",
  });

  abortHandoffFileConsumption(claim);

  assert.equal(
    findPendingHandoffFile({
      root,
      cwd: "/repo/worktree",
      worktreeDir: "/repo/worktree",
    })?.promptPath,
    saved.promptPath,
  );
});

test("recovers an interrupted consuming claim after five minutes", () => {
  const root = fixture();
  const saved = saveHandoffFile({
    root,
    sessionId: "source",
    promptText: "continue",
    cwd: "/repo/worktree",
    worktreeDir: "/repo/worktree",
    now: new Date("2026-07-30T08:00:00Z"),
  });
  beginHandoffFileConsumption({
    root,
    promptPath: saved.promptPath,
    cwd: "/repo/worktree",
    worktreeDir: "/repo/worktree",
    consumerSessionId: "interrupted",
    now: new Date("2026-07-30T08:01:00Z"),
  });

  const recovered = findPendingHandoffFile({
    root,
    cwd: "/repo/worktree",
    worktreeDir: "/repo/worktree",
    now: new Date("2026-07-30T08:06:00Z"),
  });

  assert.equal(recovered?.promptPath, saved.promptPath);
  const replacement = beginHandoffFileConsumption({
    root,
    promptPath: saved.promptPath,
    cwd: "/repo/worktree",
    worktreeDir: "/repo/worktree",
    consumerSessionId: "replacement",
    now: new Date("2026-07-30T08:06:00Z"),
  });
  assert.equal(replacement.consumerSessionId, "replacement");
});

test("rejects exact-path consumption from a different worktree", () => {
  const root = fixture();
  const saved = saveHandoffFile({
    root,
    sessionId: "source",
    promptText: "continue",
    cwd: "/repo/worktree",
    worktreeDir: "/repo/worktree",
  });

  assert.throws(
    () =>
      beginHandoffFileConsumption({
        root,
        promptPath: saved.promptPath,
        cwd: "/repo/other",
        worktreeDir: "/repo/other",
        consumerSessionId: "successor",
      }),
    /different worktree/,
  );
});

test("rejects a prompt path outside the session-state root", () => {
  const root = fixture();
  const outside = fixture();
  const saved = saveHandoffFile({
    root: outside,
    sessionId: "source",
    promptText: "continue",
    cwd: "/repo/worktree",
    worktreeDir: "/repo/worktree",
  });

  assert.throws(
    () =>
      beginHandoffFileConsumption({
        root,
        promptPath: saved.promptPath,
        cwd: "/repo/worktree",
        worktreeDir: "/repo/worktree",
        consumerSessionId: "successor",
      }),
    /outside the session-state root/,
  );
});
