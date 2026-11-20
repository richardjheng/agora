const test = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { createRequire } = require("node:module");
const vm = require("node:vm");

function setupModel() {
  const entry = require.resolve("../extension");
  const requireExtension = createRequire(entry);
  const requests = [];
  const errors = [];
  const vscode = { EventEmitter: class {
    fire() {}
  } };
  const context = vm.createContext({
    require: (id) => id === "vscode" ? vscode : requireExtension(id),
    module: { exports: {} },
  });
  const Model = vm.runInContext(readFileSync(entry, "utf8") + "\nModel", context, { filename: entry });
  context.runJson = (args) => new Promise((resolve, reject) => {
    requests.push({ args: [...args], resolve, reject });
  });
  context.showError = (error) => errors.push(error.message);
  return { model: new Model(), requests, errors };
}

const tick = () => new Promise((resolve) => setImmediate(resolve));

test("party reads and updates run in order and a failed update does not stop the queue", async () => {
  const { model, requests, errors } = setupModel();
  const initial = model.loadParties();
  const first = model.update(["--share", "codex=2"]);
  const failed = model.update(["--share", "codex=invalid"]);
  const last = model.update(["--share", "codex=3"]);
  const refresh = model.loadParties();

  await tick();
  assert.equal(requests.length, 1);
  assert.deepEqual(requests[0].args, ["parties", "--json"]);
  requests[0].resolve({ parties: { codex: { share: 1 } } });
  await initial;
  await tick();
  assert.equal(requests.length, 2);
  assert.deepEqual(requests[1].args, ["parties", "--share", "codex=2", "--json"]);
  requests[1].resolve({ parties: { codex: { share: 2 } } });
  await first;
  await tick();
  assert.equal(requests.length, 3);
  requests[2].reject(new Error("Invalid share"));
  await failed;
  await tick();
  assert.deepEqual(errors, ["Invalid share"]);
  assert.equal(model.parties.codex.share, 2);
  assert.equal(requests.length, 4);
  assert.deepEqual(requests[3].args, ["parties", "--share", "codex=3", "--json"]);
  requests[3].resolve({ parties: { codex: { share: 3 } } });
  await last;
  await tick();
  assert.equal(model.parties.codex.share, 3);
  assert.equal(requests.length, 5);
  assert.deepEqual(requests[4].args, ["parties", "--json"]);
  requests[4].resolve({ parties: { codex: { share: 3 } } });
  await refresh;
  assert.equal(model.parties.codex.share, 3);
});

test("an older quota response cannot overwrite the latest quota state", async () => {
  const { model, requests } = setupModel();
  const old = model.loadQuota();
  const latest = model.loadQuota(true);
  requests[1].resolve({ remaining: 42 });
  await latest;
  requests[0].resolve({ remaining: 10 });
  await old;
  assert.deepEqual(model.quota, { remaining: 42 });
});
