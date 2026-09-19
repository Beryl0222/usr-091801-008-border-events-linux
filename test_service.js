"use strict";

const { spawnSync } = require("node:child_process");

// 跑全套 Python 测试：基线契约 + tests/ 下的领域与端到端用例。
const result = spawnSync(
  "python3",
  ["-m", "unittest", "-v", "service_contract",
   "tests.test_policy_lifecycle",
   "tests.test_cache_correction",
   "tests.test_events_notify",
   "tests.test_visibility_privacy",
   "tests.test_http_e2e"],
  { stdio: "inherit" },
);

if (result.error) {
  console.error(result.error.message);
  process.exit(1);
}
process.exit(result.status ?? 1);
