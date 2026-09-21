import { defineRailway } from "railway/iac";
import environment0 from "./environments/arb-production.ts";

export default defineRailway((ctx, project) => {
  if (ctx.projectName === "polymarket-arb-scanner" && ctx.isEnvironment("production")) return environment0(ctx, project);
  throw new Error("Unrecognized project/environment; refusing to plan or apply infrastructure.");
});
