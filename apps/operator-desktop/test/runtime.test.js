"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const {
  buildBackendEnvironment,
  hasYamlFiles,
  seedConfigs,
  waitForBackend,
} = require("../src/runtime");

test("backend environment uses a private dynamic port and bundled frontend", () => {
  const paths = {
    configDir: "C:\\config",
    dataDbPath: "C:\\data\\cubos.db",
    frontendDist: "C:\\CubOS\\operator-web\\dist",
  };
  const env = buildBackendEnvironment(paths, 49152, { PATH: "example" });

  assert.equal(env.CUBOS_HOST, "127.0.0.1");
  assert.equal(env.CUBOS_PORT, "49152");
  assert.equal(env.CUBOS_OPEN_BROWSER, "false");
  assert.equal(env.CUBOS_WEB_DIST, paths.frontendDist);
  assert.equal(env.PATH, "example");
});

test("seed configs are copied only into an empty config directory", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "cubos-desktop-test-"));
  const seedConfigDir = path.join(root, "seeds");
  const configDir = path.join(root, "configs");
  fs.mkdirSync(path.join(seedConfigDir, "gantry"), { recursive: true });
  fs.writeFileSync(path.join(seedConfigDir, "gantry", "seed.yaml"), "name: seed\n");

  assert.equal(seedConfigs({ seedConfigDir, configDir }), true);
  assert.equal(hasYamlFiles(configDir), true);
  fs.writeFileSync(path.join(seedConfigDir, "gantry", "second.yaml"), "name: second\n");
  assert.equal(seedConfigs({ seedConfigDir, configDir }), false);
  assert.equal(fs.existsSync(path.join(configDir, "gantry", "second.yaml")), false);
});

test("backend readiness accepts a degraded health response", async () => {
  const child = { exitCode: null };
  const response = await waitForBackend("http://127.0.0.1:49152", child, {
    timeoutMs: 100,
    fetchImpl: async () => ({ status: 503 }),
  });

  assert.equal(response.status, 503);
});

test("backend readiness reports an early process exit", async () => {
  await assert.rejects(
    waitForBackend("http://127.0.0.1:49152", { exitCode: 7 }, { timeoutMs: 100 }),
    /exited during startup with code 7/,
  );
});
