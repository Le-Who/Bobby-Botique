"""Execute the real production-source guards with controlled GitHub boundaries."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(".github/workflows/deploy.yml")
GUARDS = (
    ("classify-dependency-scope", "Verify trusted and current CI source"),
    ("build-and-deploy", "Recheck deployment source before production access"),
)


def _jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _guard(steps: list[dict], name: str) -> dict:
    matching = [step for step in steps if step.get("name") == name]
    assert len(matching) == 1, f"Expected one deployment source guard: {name}"
    return matching[0]


NODE_HARNESS = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const { script } = JSON.parse(fs.readFileSync(0, "utf8"));
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const execute = new AsyncFunction("github", "context", "core", script);
const sha = "a".repeat(40);
const valid = {
  conclusion: "success", event: "push", head_branch: "vps_testai",
  head_repository: { full_name: "Le-Who/gemaibotv2" }, head_sha: sha,
};
const scenarios = [
  { name: "current trusted push", allow: true, calls: 1 },
  ...[
    ["failed CI", { conclusion: "failure" }],
    ["cancelled CI", { conclusion: "cancelled" }],
    ["missing conclusion", { conclusion: undefined }],
    ["pull request", { event: "pull_request" }],
    ["manual run", { event: "workflow_dispatch" }],
    ["missing event", { event: undefined }],
    ["other branch", { head_branch: "main" }],
    ["missing branch", { head_branch: undefined }],
    ["fork branch with production name", { head_repository: { full_name: "fork/gemaibotv2" } }],
    ["missing repository", { head_repository: undefined }],
    ["null repository", { head_repository: null }],
    ["missing repository identity", { head_repository: {} }],
    ["missing SHA", { head_sha: undefined }],
    ["empty SHA", { head_sha: "" }],
    ["short SHA", { head_sha: "a".repeat(39) }],
    ["long SHA", { head_sha: "a".repeat(41) }],
    ["uppercase SHA", { head_sha: "A".repeat(40) }],
    ["nonhex SHA", { head_sha: "g".repeat(40) }],
  ].map(([name, patch]) => ({ name, patch, allow: false, calls: 0 })),
  { name: "missing upstream run", missingRun: true, allow: false, calls: 0 },
  { name: "rerun of an obsolete SHA", tip: "b".repeat(40), allow: false, calls: 1 },
  { name: "branch ref is not a commit", type: "tag", allow: false, calls: 1 },
  { name: "branch API fails", apiError: true, allow: false, calls: 1 },
  { name: "malformed branch response", response: {}, allow: false, calls: 1 },
  { name: "missing branch object", response: { data: {} }, allow: false, calls: 1 },
];
(async () => {
  for (const scenario of scenarios) {
    const context = {
      repo: { owner: "Le-Who", repo: "gemaibotv2" },
      payload: scenario.missingRun ? {} : { workflow_run: { ...valid, ...scenario.patch } },
    };
    const calls = [];
    const github = { rest: { git: { getRef: async (args) => {
      calls.push(args);
      if (scenario.apiError) throw new Error("controlled API failure");
      return scenario.response ?? { data: { object: {
        type: scenario.type ?? "commit", sha: scenario.tip ?? sha,
      } } };
    } } } };
    const core = { info() {}, notice() {}, warning() {} };
    let allowed = false;
    try {
      await execute(github, context, core);
      allowed = true;
    } catch (error) {
      assert(error instanceof Error, scenario.name);
    }
    assert.equal(allowed, scenario.allow, `${scenario.name}: deployment eligibility`);
    assert.equal(calls.length, scenario.calls, `${scenario.name}: branch API access`);
    for (const args of calls) {
      assert.deepEqual(args, {
        owner: "Le-Who", repo: "gemaibotv2", ref: "heads/vps_testai",
      }, `${scenario.name}: exact production branch lookup`);
    }
  }
  console.log(JSON.stringify({ passed: scenarios.length }));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""


@pytest.mark.parametrize(("job_name", "step_name"), GUARDS)
def test_deploy_source_guard_rejects_untrusted_stale_or_unverifiable_runs(job_name: str, step_name: str) -> None:
    guard = _guard(_jobs()[job_name]["steps"], step_name)
    node = shutil.which("node")
    assert node is not None, "Node.js is required to execute deployment source guards."
    environment = {
        name: value for name, value in os.environ.items() if name.upper() in {"PATH", "SYSTEMROOT", "TEMP", "TMP"}
    }
    result = subprocess.run(
        [node, "-e", NODE_HARNESS],
        input=json.dumps({"script": guard["with"]["script"]}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {"passed": 25}


def test_deploy_guards_run_before_candidate_checkout_and_production_secrets() -> None:
    jobs = _jobs()
    classify = jobs["classify-dependency-scope"]
    for condition in (
        "github.event.workflow_run.conclusion == 'success'",
        "github.event.workflow_run.event == 'push'",
        "github.event.workflow_run.head_branch == 'vps_testai'",
        "github.event.workflow_run.head_repository.full_name == github.repository",
    ):
        assert condition in classify["if"]

    initial = _guard(classify["steps"], GUARDS[0][1])
    assert classify["steps"][0] is initial
    deploy_steps = jobs["build-and-deploy"]["steps"]
    recheck = _guard(deploy_steps, GUARDS[1][1])
    prepare = next(step for step in deploy_steps if step.get("name") == "Prepare secrets for SSH")
    deploy = next(step for step in deploy_steps if step.get("name") == "Deploy to DigitalOcean VPS")
    assert deploy_steps.index(recheck) < deploy_steps.index(prepare) < deploy_steps.index(deploy)
    for job_name, step_name in GUARDS:
        job = jobs[job_name]
        guard = _guard(job["steps"], step_name)
        assert guard["uses"].startswith("actions/github-script@")
        assert "if" not in guard
        assert guard.get("continue-on-error", False) is False
        assert job.get("continue-on-error", False) is False
        assert job["permissions"]["contents"] == "read"
        checkouts = [step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@")]
        assert checkouts
        assert all(step["with"]["ref"] == "${{ github.event.workflow_run.head_sha }}" for step in checkouts)
