import assert from "node:assert/strict";
import test from "node:test";

import { isContextHandoffEnabled } from "./activation.mjs";

test("context handoff is disabled without an explicit opt-in", () => {
  assert.equal(isContextHandoffEnabled({}), false);
});

test("context handoff accepts the harness opt-in", () => {
  assert.equal(
    isContextHandoffEnabled({ COPILOT_CONTEXT_HANDOFF: "1" }),
    true,
  );
});

test("context handoff rejects other opt-in values", () => {
  for (const value of ["", "0", "true", "yes"]) {
    assert.equal(
      isContextHandoffEnabled({ COPILOT_CONTEXT_HANDOFF: value }),
      false,
    );
  }
});
