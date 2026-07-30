export function isContextHandoffEnabled(env = process.env) {
  return env.COPILOT_CONTEXT_HANDOFF === "1";
}
