import { defineRailway, github, preserve, project, service, volume } from "railway/iac";

// The egress proxy remains intentionally outside this partial until its source,
// ownership, and continued need are proven. The current importer cannot round-
// trip its TCP proxy configuration without planning a networking deletion.
export const partial = "arb-scanner";

export default defineRailway((ctx) => {
  if (
    ctx.projectId !== "66c8da70-55d7-4dbc-b84b-c200a018dc05" ||
    ctx.environmentId !== "b68bb4ec-165c-45b3-90f2-4ae798cee5d4"
  ) {
    throw new Error("This configuration is restricted to polymarket-arb-scanner / production.");
  }

  const arbScannerVolume = volume("arb-scanner-volume", { alerts: { usage: { "100": {}, "80": {}, "95": {} } }, allowOnlineResize: true, region: "europe-west4-drams3a", sizeMB: 50000 });
  const arbScanner = service("arb-scanner", {
    source: github("Team-Win-and-Make-Monney/polymarket-arb-scanner", { branch: "master", checkSuites: false }),
    build: { builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    healthcheck: "/healthz",
    healthcheckTimeout: 300,
    replicas: { "europe-west4-drams3a": 1 },
    deploy: { restartPolicyMaxRetries: 10 },
    volumeMounts: { "/data": arbScannerVolume },
    env: { BASE_TRADE_SIZE: preserve(), CORRELATED_ENABLED: preserve(), DAILY_LOSS_LIMIT: preserve(), DASHBOARD_HOST: preserve(), DASHBOARD_PASS: preserve(), DASHBOARD_REFRESH_SECONDS: preserve(), DASHBOARD_USER: preserve(), DATA_DIR: preserve(), DRY_RUN: preserve(), DYNAMIC_FEE_ENABLED: preserve(), ENABLED_EXECUTION_PLATFORMS: preserve(), EVENT_MONITOR_ENABLED: preserve(), EXECUTION_BUDGET_PER_SCAN: preserve(), EXECUTION_MODE: preserve(), FINNHUB_API_KEY: preserve(), GEMINI_API_KEY: preserve(), GEMINI_API_SECRET: preserve(), IMBALANCE_ENABLED: preserve(), KALSHI_API_KEY_ID: preserve(), KALSHI_MULTI_ENABLED: preserve(), KALSHI_MULTI_MIN_DEPTH: preserve(), KALSHI_PRIVATE_KEY_BASE64: preserve(), LOGICAL_ARB_ENABLED: preserve(), LOG_LEVEL: preserve(), MAX_DAILY_TRADES: preserve(), MAX_OPEN_POSITIONS: preserve(), MAX_RESOLUTION_DAYS: preserve(), MAX_TRADE_SIZE: preserve(), MIN_PROFIT_AMOUNT: preserve(), MIN_PROFIT_THRESHOLD: preserve(), MM_ENABLED: preserve(), MM_MAX_INVENTORY: preserve(), MM_MIN_SPREAD: preserve(), MULTI_CROSS_ENABLED: preserve(), MULTI_CROSS_MIN_DEPTH: preserve(), NEWS_SNIPE_ENABLED: preserve(), OPP_SYNC_ENABLED: preserve(), PAPER_WINDOW_START: preserve(), POLYGONSCAN_API_KEY: preserve(), POLYGON_RPC_URL: preserve(), POLYMARKET_FUNDER_ADDRESS: preserve(), POLYMARKET_PRIVATE_KEY: preserve(), POLYMARKET_SIGNATURE_TYPE: preserve(), RESCAN_INTERVAL: preserve(), REVAL_FLOOR_L1: preserve(), REVAL_FLOOR_L2: preserve(), REVAL_FLOOR_L3: preserve(), REVAL_FLOOR_L4: preserve(), REWARDS_ENABLED: preserve(), SENTRY_DSN: preserve(), SNAPSHOT_ENABLED: preserve(), SUPABASE_SERVICE_KEY: preserve(), SUPABASE_URL: preserve(), SXBET_API_BASE_URL: preserve(), SXBET_API_KEY: preserve(), SXBET_PRIVATE_KEY: preserve(), SXBET_PROXY_URL: preserve(), SXBET_RATE_LIMIT: preserve(), TELEGRAM_BOT_TOKEN: preserve(), TELEGRAM_CHAT_ID: preserve(), TIME_DECAY_ENABLED: preserve(), WEBHOOK_URL: preserve(), WHALE_COPY_ENABLED: preserve() },
  });
  return project("polymarket-arb-scanner", {
    resources: [arbScanner, arbScannerVolume],
  });
});
