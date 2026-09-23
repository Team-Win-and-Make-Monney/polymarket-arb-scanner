import { defineRailway } from "railway/iac";
import targets from "./audited-targets.json" with { type: "json" };
import environment0 from "./environments/arb-production.ts";

export default defineRailway((ctx, project) => {
  const target0 = targets["arb-production"];
  if (ctx.projectId === target0.projectId && ctx.environmentId === target0.environmentId
      && ctx.projectName === target0.projectName && ctx.isEnvironment(target0.environmentName)) {
    return environment0(ctx, project);
  }
  throw new Error("Unrecognized project/environment identity; refusing to plan or apply infrastructure.");
});
