# Design Document Contract

Generated from `contracts/design-document-contract.json`; edit that source.

Use a tailored arc42 organization. Preserve an adequate project layout by
mapping these topics to its headings; consolidate related sections when
useful.

Account for every topic with evidence. Distinguish implemented facts, accepted
target design, assumptions, and unverified coverage. Conditional topics may be
inapplicable only with a reason.

The required, optional, and unnecessary criteria below are this skill's
tailoring policy. Include a view only when it materially improves explanation;
use prose for trivial relationships. Keep names, boundaries, directions,
protocols, cardinality, and runtime ordering consistent with prose and
evidence. Give each diagram a title stating its type and scope and a legend;
label element types, responsibilities, and relationship intent, with
technologies and protocols at the appropriate abstraction level.

Treat system context, container, and deployment views as C4-recommended
defaults. Omit one only when the document records why its intended readers and
scope do not benefit; component, dynamic, and code views remain value-driven.

Mechanical validation checks declarations and syntax only. Section presence,
declared coverage, link existence, and rendered Mermaid never prove
architectural quality, behavioral truth, security, or complete review.

The wording and template are original. IEEE 1016-2009 is an inactive-reserved
historical completeness reference; its public official scope supports design
information and stakeholder communication. The licensed full text was not
inspected, so do not assert clause-level conformance.

## Content Coverage

| Topic | Applicability | Content |
| --- | --- | --- |
| `purpose` | Required | State purpose, scope, stakeholders, intended readers, prerequisite knowledge, goals, and non-goals; identify the design questions readers must decide and prioritize the architectural quality goals that drive them. |
| `constraints` | Required | Separate imposed constraints from assumptions; give assumptions an owner or a way to verify them. |
| `context` | Required | Describe the system boundary, users, external dependencies, trust boundaries, and the responsibilities on each side. |
| `strategy` | Required | Explain the approach, its important tradeoffs, and how it supports goals under the constraints. |
| `building_blocks` | Required | Identify major applications, stores, modules, responsibilities, interfaces, and implementation locations at useful levels of detail. |
| `runtime` | Required | Explain important successful and failing sequences, state changes, concurrency, retry or idempotency behavior, and recovery when relevant. |
| `deployment` | Required | Map executable units to environments and infrastructure; cover configuration, rollout, observability, and operational ownership. For a library or local tool, state its host/runtime expectations. |
| `data` | Conditional | When state is managed, explain the data model, persistence, schema authority, lifecycle, migration, compatibility, and rollback or irreversible transitions. |
| `interfaces` | Conditional | When interfaces exist, link their authoritative contracts and describe authentication, payloads, errors, versioning, timeouts, and compatibility obligations. |
| `protection` | Conditional | Assess security, privacy, reliability, backup, and recovery applicability individually; describe relevant trust and access controls, sensitive data handling, failure containment, retention, restore objectives, and evidence. Do not waive this whole topic merely because one part is inapplicable. |
| `decisions` | Required | Record consequential architectural decisions, status, reasoning, and rejected alternatives; link accepted ADRs without duplicating their authority. |
| `quality` | Required | Define measurable scenarios with stimulus, operating conditions, affected element, expected response, threshold, and verification method; distinguish desired targets from measured results. |
| `risks` | Required | Record concrete risks, technical debt, known limitations, consequence, mitigation or acceptance, and ownership; do not silently treat unknown behavior as supported. |
| `glossary` | Required | Define domain terms and link authoritative references. Use one identity for each architectural element across prose, diagrams, and implementation. |
| `verification` | Required | Map material design claims and quality scenarios to behavior tests, integration or migration checks, operational evidence, and remaining verification gaps. |
| `governance` | Required | Name the document owner, authoritative path, source-of-truth hierarchy, and update triggers. Explain how code, APIs, data, deployment, decisions, and quality changes trigger a design review. |

## C4 View Selection

| View | Required | Optional | Unnecessary |
| --- | --- | --- | --- |
| C4 system landscape | The document covers a portfolio or organisation and cross-system ownership or dependencies cannot be understood from per-system context views. | Several peer systems benefit from one shared portfolio map. | The document concerns one software system and its external dependencies are clear in its system-context view. |
| C4 system context | Multiple actors or external systems, or a significant trust or ownership boundary, make a prose-only account ambiguous. | One simple external interaction still benefits stakeholder communication. | The system is isolated or its boundary and single interaction are already clear in concise prose. |
| C4 container | Several applications or stores interact and the distribution of responsibilities or communication materially affects the design. | A simple application/store split is useful to the intended readers. | One executable unit has no meaningful internal application/store split; a C4 container is not synonymous with a Docker container. |
| C4 component | A critical internal boundary or collaboration inside one container cannot be adequately explained by its container view and prose. | A stable internal decomposition helps developers make an actual design decision. | It would restate file lists or code structure without explaining a consequential relationship. |
| C4 code | A critical implementation structure cannot be adequately explained at component level and the view can be generated from authoritative source. | An important complex area benefits from a generated class, entity, or dependency view. | For ordinary code structure or whenever a hand-maintained diagram would duplicate source code. |
| C4 deployment | Distribution, environment differences, replication, failover, or a network/trust boundary materially affects operation. | A simple host/runtime mapping benefits operators. | A local tool or library has one obvious host arrangement adequately stated in prose. |
| C4 dynamic or sequence | Ordering, concurrency, asynchronous handoff, retry, transaction boundaries, or recovery has consequential interactions that prose cannot explain clearly. | A recurring user journey benefits from a compact ordered interaction view. | The operation is a trivial linear call or would duplicate an already sufficient sequence. Use one notation per scenario. |

## Authoritative References

Captured 2026-10-11. Recheck official sources for concrete
standards ambiguities; do not perform a standards refresh on every invocation.

- [arc42 overview](https://arc42.org/overview/):
  Primary organization: twelve tailorable architectural topics; testing and
  maintenance are explicit extensions here.
- [arc42 documentation](https://arc42.org/documentation/):
  Tailor the depth to stakeholder needs and keep documentation alongside
  code.
- [C4 diagram overview](https://c4model.com/diagrams):
  Select valuable abstraction levels rather than requiring all four static
  views.
- [C4 notation](https://c4model.com/diagrams/notation):
  Source for diagram titles and scope, legends, element types and
  responsibilities, relationship direction and intent, technologies, and
  protocols.
- [C4 system landscape](https://c4model.com/diagrams/system-landscape):
  Map several software systems and people across a portfolio or organisation
  boundary.
- [C4 system context](https://c4model.com/diagrams/system-context):
  Model people and external software around one system boundary.
- [C4 container](https://c4model.com/diagrams/container):
  Model applications and stores, their responsibilities, and communications;
  deployment is a separate view.
- [C4 component](https://c4model.com/diagrams/component):
  Decompose one container only when doing so adds value.
- [C4 code](https://c4model.com/diagrams/code):
  Use only for important complex implementation structure, preferably
  generated from source.
- [C4 deployment](https://c4model.com/diagrams/deployment):
  Map system or container instances to infrastructure in a named
  environment.
- [C4 dynamic](https://c4model.com/diagrams/dynamic):
  Use ordered interactions sparingly for consequential runtime patterns.
- [IEEE 1016-2009 official record](https://standards.ieee.org/ieee/1016/4502/):
  Inactive-reserved since 2020-03-05. Public scope supports stakeholder-
  oriented software design descriptions, including reconstruction from
  implementation; licensed clauses were not inspected.
