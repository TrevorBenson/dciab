# ADR 0006: The IaC path — OpenTofu and Terramate, tested in Forgejo, gated and applied by Terrakube

- Status: proposed
- Date: 2026-11-22

## Context

Kubernetes objects reach clusters through Argo CD. Everything else that has an API — DNS zones in PowerDNS, IP ranges in NetBox, Authentik applications and groups, OpenBao mounts and policies, hypervisor resources, a site's external systems — needs infrastructure as code with the same review, policy and audit as GitOps.

[COMPONENTS.md](../COMPONENTS.md) lists OpenTofu as the engine, and Terramate, Terratest and Terrakube with OPA as proposed. They must work with Forgejo (repos, webhooks, status checks), Authentik (login, [ADR 0004](0004-identity-and-sso.md)), OpenBao (provider credentials, [ADR 0005](0005-secrets.md)), and CloudNativePG or S3 for storage.

## Decisions

### 1. The path from commit to apply

| Step | Where | Tool | Result |
| --- | --- | --- | --- |
| Author | Forgejo repo | OpenTofu modules and stacks, organized and orchestrated with Terramate | Stacks with explicit dependencies |
| Detect change | Forgejo Actions, on every pull request | `terramate list --changed` | The stacks the change touches, nothing else |
| Test | Forgejo Actions | `tofu fmt -check`, `tofu validate`, then Terratest for changed modules, against disposable resources | Status check on the pull request |
| Plan and policy | Terrakube, triggered by a Forgejo webhook | `tofu plan`, then OPA policies on the plan | Plan and policy result posted back as a status check |
| Approve | Forgejo (review) and Terrakube (apply approval where policy asks for one) | | A merged change with an approved plan |
| Apply | Terrakube, after merge | `tofu apply` of the reviewed plan | New state, recorded run |

Merging to the default branch is required before an apply. Branch protection in Forgejo requires the test and the Terrakube plan/policy checks.

**The pull request plan is for review only.** After merge, Terrakube plans again from the merged commit against the current state, runs OPA on that new plan, and applies only that plan (after approval where policy asks for one). If it differs from what was reviewed, policy can require an approval, but nothing that wasn't in the merged tree is applied.

**Pull request runs are untrusted.** A pull request can change providers and data sources, and plans run that code. So pull request plans get read-only credentials (OpenBao roles that can read but not change the provider's backend), providers come only from the site's provider mirror, and the executor's egress is limited to the providers' APIs. Apply credentials are only issued to runs of the default branch.

### 2. Terramate authors; Terrakube runs

Terramate owns the repository layout, stack metadata, ordering and change detection. It doesn't hold state or run applies. Terrakube owns runs, approvals, state and the run history. One Terrakube workspace maps to one Terramate stack, created from the stack's metadata, so adding a stack in Git adds its workspace.

### 3. OPA is the gate between plan and apply

Policies are Rego, kept in their own Forgejo repo and versioned like code. They read the plan as JSON. Examples: no resource deletions without an apply approval; no changes to protected stacks (OpenBao, Authentik core, DNS for the platform domain) outside the platform team; required tags and naming. A failed policy stops the run before apply and fails the status check.

### 4. Storage

- Terrakube's own database runs on CloudNativePG.
- State and run outputs go to Terrakube's storage backend, S3-compatible: SeaweedFS on INFRA by default, or the site's own S3. Versioning and encryption at rest are on, only Terrakube's own credential can read the bucket, and the bucket is in the backup set that leaves the site (encrypted).
- State can hold secret values, even ones marked sensitive. Modules prefer ephemeral values and write-only attributes where providers support them, and outputs never carry secrets. Run logs and state are visible only to the workspace's team in Terrakube.

### 5. Credentials

- **Provider credentials** come from OpenBao at run time: the Terrakube executor authenticates with its Kubernetes service account (or a JWT-SVID, [ADR 0004](0004-identity-and-sso.md)) and reads short-lived dynamic credentials where the provider's backend supports them, KV secrets otherwise. No credential is stored in Terrakube variables or in Git.
- **Forgejo Actions** use their own narrow OpenBao roles for test resources, never the apply credentials.
- **People** log in to Terrakube through Authentik. Groups map to Terrakube teams; `spec.identity.adminGroup` is the admin team.

### 6. Forgejo integration

Terrakube connects to Forgejo as a VCS provider: a webhook per repository triggers plan runs on pull requests and apply runs on merge, and results are posted back as commit status checks. If Terrakube's Forgejo support (through its Gitea-compatible or generic Git integration) falls short for status checks, a small Forgejo Actions job calls Terrakube's API and posts the status instead; the spike decides which.

### 7. Drift

A scheduled Terrakube plan runs for every workspace. A non-empty plan opens a Forgejo issue on the stack's repo; it's never applied automatically.

## Consequences

- Two systems hold state about IaC: Forgejo (code, reviews, checks) and Terrakube (runs, state, approvals). Git stays the source of truth for intent.
- Terrakube is a service the platform team runs and upgrades, with its database and bucket in the backup set.
- Platform resources DCiaB creates during the bootstrap are imported into stacks at the platform step, so later changes go through this path.
- The Forgejo integration (decision 6) needs a spike before Terrakube joins the plan; it is the remaining part of the "IaC run platform" decision in COMPONENTS.md.
