# ADR-0025 — An Approved eHall Certificate Pipeline

## Title

One whitelisted eHall errand — the certificate application — prepared read-only, approved exactly,
and submitted once by a typed pipeline that cannot become a general browser agent.

## Status

Accepted

## Context

Six phases built a boundary that can send an approved mail. The obvious next capability is a
browser: the university's eHall is where several errands actually happen, and a browser can reach
all of them. That is exactly the problem.

- **a browser is an unbounded capability.** If the project exposes "open a page and click", then
  every restriction that follows is a policy, not a boundary — and a policy that lives in a prompt
  or a configuration list is not something a user can verify;
- **the interesting eHall operations are irreversible.** Dropping a course, withdrawing an
  application, cancelling a dorm booking. A mistake is not a bad answer; it is a change to the
  user's enrolment record;
- **the page is not under our control.** The university may rename a service, reorder fields or
  change what a form accepts at any time, and a pipeline that adapts silently would submit
  something the user never reviewed;
- **preparing must not touch anything.** A form that is filled starts autosaving, validates, and
  may hold server-side state before a person has approved a single value;
- **login is the user's, not ours.** An SSO password is the single most valuable credential in the
  user's life; a project that stores one to automate a form has traded a great deal for very
  little.

Phase 6A already provides the mechanism this needs: an immutable `ActionRequest`, an approval bound
to its exact fingerprint, one consumed approval per execution, and an executor registry in which
capabilities exist only when they are written. Phase 6C adds exactly one capability to that
registry.

## Decision

1. eHall is accessed through a dedicated typed pipeline, never a generic browser tool.
2. The first and only production eHall capability is `ehall.submit-certificate`.
3. Drop-course, cancellation, withdrawal, dorm checkout, deletion and arbitrary-form capabilities
   do not exist.
4. The model has no browser or eHall capability.
5. eHall login is manual; the assistant never receives or fills the university password.
6. Browser session state lives under the XDG runtime data directory, outside Git.
7. Preparation is read-only with respect to the remote form: inspect the schema and materials, but
   do not type or submit.
8. The user supplies form values explicitly.
9. Phase 6C performs no automatic personal-fact autofill.
10. A preparation snapshots the exact service contract, fields, values, materials and consequence
    into one immutable `ActionRequest`.
11. The action type is exactly `ehall.submit-certificate`.
12. Browser execution may only occur after the existing exact human approval is consumed.
13. The executor re-opens the whitelisted service and verifies the page contract before filling.
14. A changed or mismatched form structure fails closed.
15. The executor fills only the exact approved values.
16. The executor reads back filled values before clicking submit.
17. The only external side-effect click is the whitelisted final submit operation.
18. Failure before submit is a definite `FAILED`.
19. Failure after submit is `UNKNOWN` unless the page states a decisive result.
20. `UNKNOWN` is never automatically retried.
21. Required unsupported controls and uploads block preparation.
22. No generic selector or click API crosses the adapter boundary.
23. Unexpected top-level navigation outside the NJU allow-list fails closed.
24. Phase 6C does not learn or promote browser workflows automatically.

Additional frozen details:

- **One package owns Playwright.** `adapters/ehall/` is the only place `playwright` is imported, and
  an architecture test enforces it. The application sees two operations — `inspect_form` and
  `submit_certificate` — and a `Port`-level test pins that name set, so a `goto`, a `click` or a
  `fill` cannot be added to the boundary without deleting that test.
- **The service is a constant.** The pipeline looks for the exact title `证明书申请` and requires
  the page markers it was reviewed against. Zero matches, several matches and a missing marker all
  raise `EHallServiceMismatch`. The CLI cannot pass a service name, a service id or a URL.
- **The page contract is the approval's meaning.** A canonical SHA-256 over the pipeline id, the
  contract version, the service identity, the required page markers, the ordered field definitions
  (key, label, kind, required, options), the required materials and the submit control. Field keys
  prefer a stable page attribute and otherwise fall back to `field-NN` — but the ordinal is part of
  the fingerprint either way, so a reordered form invalidates the prepared action.
- **Preparation never types.** `pw ehall certificate prepare` inspects, validates locally against
  the live contract, builds the payload and stores the action. The remote form is untouched, so the
  portal's own autosave and validation never see a value the user has not approved. All values come
  from `--field KEY=VALUE`: no knowledge lookup, no mail analysis, no model, no `ConfirmedFact`.
- **The payload carries no secrets or selectors.** `schema_version`, `pipeline_id`,
  `page_contract_version`, `page_contract_fingerprint`, `service_identity`, the reviewed fields,
  the required materials and a fixed local `consequence` sentence. `from_payload` is strict, so a
  cookie, a session path, a selector or a password cannot appear without changing the schema.
- **The consequence is local text.** "Submitting this action creates a certificate application in
  the NJU eHall and may create an administrative record visible to university staff." Neither the
  model nor the page writes it, and `EHallCertificatePayload` refuses anything else.
- **Execution re-verifies everything, then types.** Open the whitelisted service, re-read the
  contract, require the fingerprint to equal the approved one, validate every value against the
  live options *before* typing, fill only the approved values, read every value back and require an
  exact match, then click the one submit control whose identity is part of the contract.
- **Outcome semantics follow the click.** Anything before the click — a changed contract, an
  expired session, a page this pipeline does not recognise, a readback mismatch — is `FAILED` with
  `submitted=False`, and the run records a definite failure. From the moment the click is
  dispatched (including a Playwright timeout that may have fired after the mouse event) the result
  is `SUCCEEDED` only on an explicit accepted state, `FAILED` only on an explicit rejection, and
  `UNKNOWN` otherwise.
- **No retry, no guess.** There is no resubmission path in the adapter, no scheduled retry, no
  `ehall-worker` in the daemon, and no reconciliation that infers "not submitted" from "I do not
  see it in my applications". An `UNKNOWN` attempt consumes its approval and blocks further
  execution of that action under the Phase 6A unresolved fence; the CLI says to inspect the portal
  by hand and not to submit again blindly.
- **Session expiry is a definite failure.** If the session has expired before anything is typed,
  the run fails with a message telling the user to run `pw ehall login`; the approval has already
  been consumed because execution began, so another attempt needs a new approval.
- **The profile is not a secret.** Cookies live in
  `$XDG_DATA_HOME/growing-assistant/ehall/nju-profile/`, created owner-only, and are never copied
  into the repository, the cache or a payload. `pw ehall status` prints the path but never claims
  the session is still valid.
- **The login is manual and code-free.** `pw ehall login` opens a headed Chromium at the eHall home
  page and waits. The session module has no `fill`, no `type`, no `get_by_label` and no identifier
  named `password` or `credential` — a test walks its syntax tree to prove it.
- **Top-level navigation is allow-listed.** `ehall.nju.edu.cn`, `ehallapp.nju.edu.cn` and
  `authserver.nju.edu.cn` over HTTPS. Sub-resources, fonts and CDNs are ordinary web traffic and
  are not restricted; a *top-level* navigation anywhere else raises `EHallUnexpectedOrigin`.
- **No new durable state.** The `ActionRequest` payload, the approval, the run and the case are
  enough, so migrations still end at `0011_approved_mail_send.sql`.

## Alternatives Considered

- **A generic browser tool with an allow-list of actions.** The capability would exist; only its
  policy would forbid misuse. Rejected; the capability set is the boundary.
- **A generic eHall form driver.** Any service could be driven by passing a different name.
  Rejected; one pipeline, one service, one action type.
- **Automate the SSO login.** It would require holding the user's university password. Rejected;
  the browser is headed and the login is manual.
- **Fill the form during preparation.** It would trigger server-side autosave and validation before
  approval. Rejected; preparation is read-only.
- **Autofill from the knowledge base or the mail.** The user would be approving values a model
  produced from their private data. Rejected; every value is explicit input, and fact trust is a
  separate design.
- **Submit and then reconcile by looking for the application in "my applications".** Absence there
  is not evidence about a click that already happened. Rejected; no automatic reconciliation.
- **Retry a failed submit automatically.** The click may have landed. Rejected (ADR-0023, ADR-0024).
- **Resolve a changed page by adapting to the new form.** It would submit fields the user never
  reviewed. Rejected; a contract change is a refusal.
- **Let the model read the page and decide what to click.** The pipeline's safety comes from the
  page being a *contract*, not from a model's judgement about a DOM. Rejected.
- **A configuration key for the service URL or the submit selector.** It would make the whitelist a
  preference. Rejected; the pipeline owns them, and the parser rejects such keys.

## Consequences

- One real errand becomes automatable end to end — inspect, prepare, approve, submit — with the
  approval still meaning exactly what it meant for mail: these bytes, once.
- The pipeline is testable without a browser. The gateway's policy runs against a fake page, so
  service selection, markers, the contract fingerprint, fill/readback and the outcome split are all
  covered in CI, while Chromium is only needed to actually use the feature.
- Costs and limits: the portal UI is not a contract, and a page-contract mismatch stops the
  pipeline until the adapter is reviewed and updated; a required upload ends the errand in the
  browser rather than faking a file; an ambiguous result needs a person to look at the portal; and
  the browser runs headed, so this capability is only usable on a machine with a display.
