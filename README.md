# 🧯 SRE Incident Copilot

**Storage-pressure alert triage for on-call engineers.** Paste an Alertmanager alert (or just describe it) and get a
risk-ranked, safety-checked response plan: how urgent it really is, what to check first, what is safe to do now,
what must not be done without approval, and a ready-to-send incident update.

Built at **Hacktoberfest Hack Day Barcelona 2026** · theme *Expertise → AI* · tracks: **Best Open-Source AI Project**, **Best Use of Gemma 4**.

> Decision support only. The Copilot never runs commands, never touches servers, databases or Kubernetes.
> The engineer stays responsible for every action.

## 1. What it is and who it is for

For SREs, DevOps / platform engineers and on-call people who get paged with
`NodeFilesystemAlmostOutOfSpace`, `KubePersistentVolumeFillingUp` or `MongoDBDiskSpaceLow` at 3am.

A plain LLM explains the alert. An experienced SRE asks different questions: *how many hours of runway do we have?
Is this the primary? Will deleting data even free disk? Can this StorageClass expand at all?* The Copilot packages
those questions and the hard-learned "never do this" rules into a pipeline around Gemma 4:

```
Alert JSON / text
  → Normalizer          parses Alertmanager JSON, units (GiB, MiB/min), metrics from text      src/normalizer.py
  → Risk analyzer       deterministic used %, time-to-exhaustion, urgency policy (no LLM math)  src/risk_analyzer.py
  → Change connector    releases, commits, Jira, Slack, calendar correlated by host/service    src/changes.py
  → Expert rule engine  picks matching YAML rules (Linux / Kubernetes / MongoDB / global)       src/expert_engine.py
  → Incident history    similar past incidents + their outcomes (helped / no effect / worse)   src/feedback.py
  → Gemma 4             writes the plan from facts + rules; alert text is fenced as untrusted   src/pipeline.py, src/llm_provider.py
  → Output guard        schema check, real rule ids only, urgency from code, dangerous          src/output_validator.py
                        actions and actions that made things worse before forced to "needs approval"
  → UI                  plan, missing context, trace, side-by-side with plain Gemma,            app.py
                        "close the loop" form that records the outcome
  → Prepare fix         level 1: commands, dry-run, verify, rollback for one action;          src/remediation.py
                        allow-list + guard decide the status. Nothing is executed.
```

**Change connector.** Looking around for releases and recent changes is the most often missed step, so the Copilot
does it automatically. In the MVP it reads a synthetic feed ([`context/changes_feed.json`](context/changes_feed.json))
in the shape a real git / CI-CD / Jira / Slack connector returns, and correlates events with the alert by host,
replica set, namespace, PVC and service names (48h window; unrelated network work and frontend deploys are filtered out).
For the MongoDB scenario it finds the `orders-service` deploy, the commit "add order_events collection, TTL later"
and the backfill announced in Slack, and the plan names them as the likely cause.

**Prepare fix (level 1).** Each immediate action has a 🛠 *Prepare fix* button. Gemma writes the exact commands:
a dry-run or preview, the change, verification with expected output, and a rollback, plus what could get worse and the blast radius.
Code decides the status, not the model: an action that matches the
[remediation allow-list](expertise/remediation_allowlist.yaml) (pause a writer, rotate logs, expand a PVC, revert a change…)
is marked as a candidate for a future one-click fix. A dangerous command (`dangerous_actions` in the rules) makes it
"needs a human decision". The *Apply* button is visible but disabled: execution is level 2.

**Feedback loop.** After the incident the engineer records which actions were taken and whether they
✅ helped, ➖ had no effect or ❌ made it worse (saved to `feedback/incidents.jsonl`). On the next similar alert
(same component and category, ranked by alert name, host and bad outcomes first) the past incidents go into the prompt,
and code forces human approval on any action that resembles one that made things worse before.
The repo ships 5 synthetic past incidents ([`feedback/seed_incidents.jsonl`](feedback/seed_incidents.jsonl)) so the demo shows the effect:
for the MongoDB scenario the Copilot first suggests pausing the noisy writer (it helped in `inc-seed02`) and flags
`compact` with "made it worse in `inc-seed01`".

Scope of the MVP: **storage pressure** on Linux filesystems, Kubernetes PVCs and MongoDB (WiredTiger, replica sets).

## 2. How to run

Requires Python 3.12 and a free Gemini API key ([aistudio.google.com/apikey](https://aistudio.google.com/apikey)).

```bash
git clone https://github.com/izmal/sre-incident-copilot && cd sre-incident-copilot
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # put your GEMINI_API_KEY there
streamlit run app.py
```

Open http://localhost:8501, pick a demo scenario (or paste your own alert) and press **Analyze**.
First useful answer: 3 clicks, ~20-35 seconds.

Reproduce the evaluation (22 Gemma calls; responses are cached in `evaluation/cache/`):

```bash
python -m evaluation.evaluate            # writes evaluation/results/summary.md and results.json
pytest -q                                # deterministic parts, no API key needed
```

## 3. Expertise: what is built in and where it comes from

All expertise is explicit, versioned YAML in [`expertise/`](expertise), not hidden in a prompt.

**The author's on-call workflow** (the part that separates an SRE from "disk is full → add disk") is encoded as global rules
applied to every alert:

1. **Impact first** (`global.impact.001`): is anyone suffering right now, customers or internal teams? Real impact → fastest *safe* fix first; no impact and hours of runway → do it properly.
2. **Fix fast, but don't make it worse** (`global.harm.001`): the obvious fix often cascades. Example from practice: a backend throws 500s for lack of CPU, the team scales it up, the extra instances overload the database and every dependency goes down, when rate limiting was the right move. For storage: resize / compact / resync add I/O to a node that is already struggling.
3. **Look around** (`global.context.001`): a release in progress (maybe the errors are expected), a recent change by any engineer (dev, infra, network, DBA), an external event (campaign, launch, provider incident). This is the most often missed context.
4. **Trends and past incidents** (`global.history.001`): compare with last week and with earlier incidents under similar load: what was done, did it help?
5. **Close the loop** (`global.learning.001`): record whether the recommendations helped or made things worse, so good decisions become rules and bad outcomes become constraints.

| File | What it encodes |
|---|---|
| [`global_principles.yaml`](expertise/global_principles.yaml) | The 5 workflow rules above, the urgency policy (TTE / headroom / inode / impact thresholds as config), "destructive ops are never quick fixes", "don't default to adding disk", "no invented numbers" |
| [`mongodb_storage.yaml`](expertise/mongodb_storage.yaml) | Deleting docs doesn't free OS space (WiredTiger reuse), `compact` is not an emergency fix, primary vs secondary blast radius, leave the oplog alone, find the growth source, never touch dbPath |
| [`linux_storage.yaml`](expertise/linux_storage.yaml) | Forecast from the time series not the %, inode exhaustion is a different incident, deleted-but-open files, rotate don't delete logs, reserved blocks, never clean DB data dirs |
| [`kubernetes_storage.yaml`](expertise/kubernetes_storage.yaml) | PVC expansion preconditions (StorageClass + CSI), StatefulSet volumeClaimTemplates, never delete a PVC to "recreate it bigger", look inside the workload |

Each rule has a `rationale`, `checks` (concrete commands / PromQL), `constraints`, `required_context` (drives the
"missing context" list) and `dangerous_actions` (regexes the output guard enforces in code).

**Source:** the author's hands-on production SRE/DevOps experience (incident response, capacity planning, MongoDB and
Kubernetes operations), cross-checked with official docs: [MongoDB storage FAQ](https://www.mongodb.com/docs/manual/faq/storage/),
[WiredTiger](https://www.mongodb.com/docs/manual/core/wiredtiger/), [compact](https://www.mongodb.com/docs/manual/reference/command/compact/),
[Kubernetes PV](https://kubernetes.io/docs/concepts/storage/persistent-volumes/) / [StorageClass](https://kubernetes.io/docs/concepts/storage/storage-classes/),
[Prometheus functions](https://prometheus.io/docs/prometheus/latest/querying/functions/). No internal runbooks or real incident data are included;
all scenarios are synthetic.

The full product concept (in Russian) is in [`docs/CONCEPT.ru.md`](docs/CONCEPT.ru.md).

## 4. Model and license

- **Gemma 4** via the **Gemini API**: default `gemma-4-26b-a4b-it`, optional `gemma-4-31b-it` (set `GEMMA_MODEL`).
  Called in [`src/llm_provider.py`](src/llm_provider.py) with JSON output and `thinking_level="minimal"`.
- Gemma 4 is released under the [Apache 2.0 license](https://ai.google.dev/gemma/apache_2) ([model card](https://ai.google.dev/gemma/docs/core/model_card_4)).
  Gemini API use is subject to the [Gemini API terms](https://ai.google.dev/gemini-api/terms).
- This project's code: [MIT](LICENSE).

## 5. Proof: better than the same model without it

Same model, same input, same output JSON schema, same temperature. The only difference is the Copilot layer.
11 synthetic scenarios ([`evaluation/cases.json`](evaluation/cases.json)): fast vs flat growth, primary vs secondary,
"deleted docs but disk didn't shrink", inode exhaustion, deleted-but-open log, PVC with/without expansion,
a bare alert with no metrics, and a prompt-injection attempt in the alert annotation.

Scoring is automatic and reproducible ([`evaluation/rubric.md`](evaluation/rubric.md)): urgency, key risks, safety,
diagnostic plan, handling of unknowns, 0-2 each, max 10. A **critical safety error** (a destructive action proposed as an
immediate step without human approval) is counted separately.

**Results (`gemma-4-26b-a4b-it`, 11 cases):**

| Mode | Avg score /10 | Correct urgency | Unknowns listed (sum, max 22) | Critical safety errors |
|---|---|---|---|---|
| Plain Gemma 4 (baseline) | **6.7** | 4/11 | 4 | 0 |
| Gemma 4 + SRE Incident Copilot | **8.9** | 11/11 | 14 | 0 |

What made the difference:

- **Urgency calibration.** Plain Gemma escalated 7 of 11 cases, mostly to the monitoring `severity` label: it called
  "95% but flat for 7 days" `high` and a bare "disk alert, severity critical" `critical`. The Copilot computes time-to-exhaustion
  in code and says `unknown` when there is no growth data.
- **Asking for the right missing data**: replication lag, the state of the other members, expansion preconditions.
- **Domain traps**: after "we deleted 50M docs and disk didn't shrink" the Copilot explains WiredTiger space reuse and
  checks `freeStorageSize`. The baseline rated it `high` and missed why.

The author's workflow, measured separately (not part of the /10 score):

| Check | Plain Gemma | Copilot |
|---|---|---|
| Asks whether users / business are affected right now | 3/11 | **11/11** |
| Checks recent releases / changes / external events | 4/11 | **11/11** |
| Says what could get worse for every action that changes something | 11/11 | 11/11 |
| Compares with trend / last week / past incidents | 5/11 | **8/11** |

Honest notes: on this set plain Gemma made **no** critical safety errors either (it also ignored the prompt injection),
so the safety guard is a backstop here, not the measured win. The guard changed one answer (removed a rule id that did not apply).
The expert answers are ~10s slower (~34s vs ~24s).

Full table: [`evaluation/results/summary.md`](evaluation/results/summary.md). Raw outputs: [`evaluation/results/results.json`](evaluation/results/results.json).

## Where it does not work (limitations)

- Only storage-pressure incidents (Linux FS, K8s PVC, MongoDB). Other alerts get generic rules and will say so.
- Time-to-exhaustion is linear from a single growth rate you provide; it does not query Prometheus yet. No growth rate → urgency `unknown`, by design.
- Free-text metric extraction is regex-based; structured `observed_metrics` is more reliable.
- Rules reflect one engineer's experience and need review for your environment (thresholds are config).
- The rubric is keyword-based: it checks that key points are present, not that the prose is perfect. 11 cases are indicative, not proof.
- The change feed is synthetic; correlation is by name matching (host, service, namespace), so a change to a dependency with a different name is missed. Prepared commands use `<placeholders>` for unknown names and must be reviewed.
- Incident history matching is simple (component + category + alert name/host); the evaluation runs **without** history and the change feed so its numbers do not depend on local feedback. The history file is local and per installation.
- Alert text is sent to the Gemini API (hosted). Use synthetic or anonymized data; secrets in alerts are not scrubbed yet.

## Safety

- Read-only: no SSH, no shell, no DB or Kubernetes API.
- Alert text, annotations and logs are untrusted: fenced in the prompt **and** the guard re-checks output in code
  (dangerous actions → `requires_approval=true`, unknown rule ids removed, urgency taken from the deterministic analyzer).
- If the API fails, the UI shows a clear error plus the deterministic analysis, never a made-up conclusion.

## Vision: from alert to fix

The target product is a runtime assistant wired into live data, not a paste box. Three stages, each with the same
principle as today: facts and safety come from code, judgement comes from expertise, the LLM connects them.

```
INPUT (live data)                 ANALYSIS (data, trends, history)            OUTPUT (from advice to a fix)
─────────────────                 ────────────────────────────────            ─────────────────────────────
Alertmanager webhooks             real metrics via read-only PromQL:          1. Action plan: what to check,
Prometheus metrics + thresholds     predict_linear, offset 1w, seasonality       what is safe, what needs approval
Releases / CI-CD / git commits    correlate the start of the problem with     2. Prepared fix: exact command,
Jira tickets, change calendar       releases, config and infra changes           PR or Jira change request,
Slack incident channels           similar past incidents and their outcomes      dry-run output, rollback step
Kubernetes / cloud state (r/o)    expert rules + impact / blast radius        3. One-click fix: only when confident
Incident history (feedback loop)  deterministic urgency, confidence score        and the action is allow-listed
```

**Levels of autonomy.** The Copilot only earns the right to act step by step, following the author's
"fix fast, but don't make it worse" rule:

| Level | What the Copilot does | When |
|---|---|---|
| 0. Advise (**today**) | Action plan, risks, missing context, incident update | Always |
| 1. Prepare (**today: 🛠 Prepare fix**) | Exact commands with dry-run, verification and rollback; a human runs them. Next: PR / change request | Read-only data confirms the diagnosis |
| 2. Fix with one click | A button that executes an allow-listed, reversible action (pause a batch job, rotate logs, expand a PVC on a StorageClass that supports it), then verifies the effect and rolls back if needed | High confidence, action in the allow-list, no rule forbids it, RBAC and target environment checked, approved by the on-call |
| Never automatic | Deleting data, dropping collections, compact / resync, deleting PVCs, restarting primaries | Always a human decision (`dangerous_actions` in YAML) |

Every executed action is logged and its outcome feeds back into the incident history, so the next recommendation
knows whether it helped or made things worse.

## Roadmap

1. **Input:** Alertmanager webhook receiver; read-only PromQL so trends come from real data (`predict_linear`, `offset 1w`), not a single growth rate.
2. **Context:** replace the synthetic change feed with real connectors (GitHub/GitLab, Argo CD, Jira, Slack), plus read-only `kubectl`, with every tool call shown in the trace.
3. **Output level 1:** besides prepared commands (done), open a PR or Jira change request with the plan attached.
4. **Output level 2:** one-click fixes for an allow-list of reversible actions, with RBAC, approval, verification and an audit log.
5. Feedback loop: turn repeated bad outcomes into proposed YAML constraints for expert review; semantic similarity; shared storage.
6. More domains: PostgreSQL WAL, Kafka retention, memory/CPU saturation, 5xx (where the "don't scale into a cascade" rule matters most). Local inference via Ollama.
