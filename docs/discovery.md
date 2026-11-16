# Technical Discovery: Incident Triage Agents

> Fill the **[TO COMPLETE]** parts from a real conversation (a senior engineer, a faculty member, or a colleague acting as stakeholder). The point of this document is evidence that you led discovery, so the answers must be genuine.

## 1. Problem statement
On-call engineers lose time at the start of an incident: reading raw logs, finding the failing code, and writing up a fix and ticket. The goal is a system that takes an alert, produces a diagnosis with a code location, and proposes a reviewed fix.

## 2. Stakeholders
| Stakeholder | Interest | Interviewed? |
|---|---|---|
| On-call engineer | Faster diagnosis, trustworthy output | [TO COMPLETE] |
| Engineering manager | Cost per incident, MTTR | [TO COMPLETE] |
| Platform / security | What data the agents may read | [TO COMPLETE] |

## 3. Discovery questions to ask (and record answers)
1. Walk me through the last incident you handled. Where did the first 30 minutes go?
2. What do you check first, and in what order? (drives the specialist agents and tools)
3. What would make you distrust an automated diagnosis? (drives the critic)
4. Which actions may an agent take on its own, and which need approval? (drives scope: this build is read-only)
5. What does a good incident ticket look like? (drives output format)
6. What would one triage run have to cost before the team stops using it? (drives cost-per-request target)
7. Which logs and repos may the agents see? (drives tool scoping)

**Answers and what changed in the design:** [TO COMPLETE]

## 4. Requirements (initial, from the author's assumptions, validate with stakeholders)
- R1. Accept an alert/incident text and return category, failing signal, code location, proposed fix.
- R2. Every proposed fix must cite real code and include a regression test; a reviewer agent enforces this.
- R3. Read-only: agents never modify code or infrastructure.
- R4. Every run emits a trace with tokens, cost, latency per step.
- R5. Cost per request and latency are tracked against targets (below).

## 5. Success metrics
| Metric | Target | Current (mock benchmark) |
|---|---|---|
| Incidents resolved (right file, approved fix) | >= 90% | 100% on 10 sample incidents (mock LLM) |
| Cost per request | [TO COMPLETE with stakeholder] | see README |
| p50 latency | [TO COMPLETE] | see README |
| Reviewer-forced revisions | tracked | 50% of sample runs |

## 6. Constraints and risks
- Prompt injection through log content: logs are untrusted data, never instructions (mitigation: tools are read-only, no write tools exist).
- Hallucinated code locations (mitigation: critic re-reads the cited file before approving).
- Runaway loops and cost (mitigation: recursion limit, max 2 revisions, max 4 ReAct steps).

## 7. Open questions
- Which real LLM provider and prices apply? (benchmark prices are placeholders)
- How should incidents without a stack trace be handled beyond the grep-based fallback?
- Should approved fixes open a draft PR automatically (needs a write-capable tool and an approval gate)?
