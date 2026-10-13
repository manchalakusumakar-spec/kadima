# KADIMA NIFTY — evening release review (NOT a deployment script)

This release is **NOT APPROVED for AWS installation** by virtue of passing tests.
Do not run a prior automation/trading installer for forecast changes.

## Evidence expected before any deployment approval

- [ ] Installed and *loaded* Python module/source/config identities verified.
- [ ] No competing deployment, broker pending order or unmanaged exposure;
      complete fresh read-only broker reconciliation and policy checks.
- [ ] Read-only native cache handoff with a lock respected by *writers*.
- [ ] Valid official dated NIFTY50 weight manifest and fresh constituent samples.
- [ ] Verified independent OI/Greeks event, receipt and availability timestamps.
- [ ] Source-specific time-of-day anomaly baselines tested; no fake shocks.
- [ ] Original issue-time forecasting and separately timestamped revisions.
- [ ] Every original saved before publication; no revised values backdated.
- [ ] Full composed parity tests for Gamma, CAS, Shield, stock-fast, intraday,
      risk, ledger, scheduler, kill switch and current Celestial display.
- [ ] CPU/RAM/disk measurements fit the existing small EC2 instance.
- [ ] Manual source-pinned plan, exact files/services, backup+rollback,
      preflight and health checks prepared for **separate explicit approval**.
- [ ] Real historical and prospective shadow evidence, no synthetic fitting
      presented as a market result; measured performance by 5–60m horizon.

If any fail, do not install or enable. The next permissible actions are
read-only inspection, offline test/PR review, and capture-only scaffolding in
an isolated worktree. This package does not change the protected
`release_plan.py --apply` refusal or PR42's staging blockers.

No task runs automatically this evening; a human must resume and approve
any specific production change after all gates pass.
