import { handle, cleanup } from "./service.mjs";

// Keep the deployed entry module limited to Worker handlers. Exported
// constants in an entry module are interpreted as named runtime entrypoints.
export default {
  fetch(request, env) { return handle(request, env); },
  async scheduled(_event, env, ctx) { ctx.waitUntil(cleanup(env)); },
};
