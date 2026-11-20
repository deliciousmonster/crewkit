# The agent SDLC

Every crew delivers a project through the same five documents. A crew keeps its own roles, tasks and
files; this page, and `crewkit sdlc`, own how requirements are gathered, how success is defined and
how progress is tracked.

## Where the documents live

One project in a workspace keeps them at the workspace root: `brief.md`, `interview.md`,
`requirements.md`, `design/` and `development.md`. Several projects each get `projects/<slug>/`
holding the same set. `crewkit sdlc` finds a project wherever a `brief.md` sits in one of those two
places, and reads nothing else in the workspace.

| Document | Written by | What it holds |
|---|---|---|
| `brief.md` | the owner, with the agent | What to deliver, for whom, informal requirements, sources, outputs, constraints |
| `interview.md` | the agent | One `Q-n` entry per question: options, a proposed answer, the owner's quoted answer, its state |
| `requirements.md` | the agent, signed by the owner | `REQ-n` entries, each with Source, Priority, Success and Acceptance; change requests `CR-n` |
| `design/` | the agent, reviewed by the owner | `index.md` (components, interactions, data contracts, infrastructure, order, success criteria, example functions, folder structure), one doc per component, and `feedback.md` with `FB-n` entries by round |
| `development.md` | the agent | `TASK-n` rows with the REQs each serves, status, ticket and evidence; acceptance rows per REQ |

Profile `light`, set in the brief's front matter, lets `design/` be `index.md` and `feedback.md`
alone.

## Stages and gates

The stage is computed from the files; nothing stores it.

1. **brief**: until `brief.md` says `status: ready`.
2. **interview**: until `interview.md` says `status: complete` and no question is `open`. The agent
   declares it; the owner confirms it by approving the requirements built from it.
3. **requirements**: until gate G1, the owner's approval of `requirements.md`.
4. **design**: until gate G2, the owner's approval of `design/`.
5. **development**: until every task is `done` or `dropped` and every `must` requirement has an
   Acceptance row whose Result starts with `pass`.
6. **acceptance**: until gate G3, the owner's acceptance of the delivery.
7. **done**.

Do not start work past the next gate. `crewkit sdlc next` says what is missing and who acts.

## Approval

`crewkit sdlc approve <requirements|design|acceptance> --by owner --words "<the owner's words>"`
writes a row in the document's `## Sign-off` table with a hash of what was approved: the
`## Requirements` section, every `design/*.md` but `feedback.md`, or the Tasks and Acceptance tables.
`--kind adopted` records an owner's approval of a document reconstructed after the work was done.
crewkit cannot tell an owner's word from an agent's claim, so a crew lets only its Lead run
`approve`, and only after the owner has answered. The hash makes a later silent edit visible to
`lint`.

Approving requirements refuses while a `must` requirement lacks Success or Acceptance, or an
`assumed` one is not listed by id under Assumptions and open items.

## Change after sign-off

`crewkit sdlc change "<title>" --affects REQ-003` appends a proposed `CR-n` under `## Changes`.
Edit the requirements, then `crewkit sdlc approve CR-n --by owner --words "..."` bumps `rev`, records
the new hash and stamps the CR with its rev. A design doc whose `requirements_rev` is below the rev
of a requirement it cites is stale until someone re-reads it and raises the number.

## Lint

`crewkit sdlc lint` exits 1 on an error, and on a warning too with `--strict`.

Errors: a deliverable missing for the current stage; a required heading missing; an unknown status;
a later document approved while an earlier one is not; a sign-off with no words; a signed section
edited since (requirements: unless a CR is proposed); a duplicate id; a task or design doc citing a
REQ that does not exist; an interview marked complete with a question open.

Warnings: a requirement with no Success or Acceptance; a `must` requirement no design doc or no task
cites; a stale design doc; a brief changed since the interview's `brief_hash`; a `done` task whose
evidence path does not exist; open feedback while the design is approved; a tracker set and a task
with no ticket; a `shared_with` copy that differs.

## Adopting an existing workspace

`crewkit sdlc init` writes the missing documents and never overwrites one. Older plans stay where
they are while the new documents are drafted from them. Then `crewkit sdlc retire <path>` moves each
to `archive/pre-sdlc/`, rewrites references to it outside `journal/`, `evidence/` and `archive/`, and
logs the move in `archive/path-moves.md`.

## Names this does not change

A target's `INTAKE.md` is a dossier on the target, not the owner's requirements. A crew's own
interviews folder, where one exists, is not `interview.md`.
