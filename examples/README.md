# Example questions

Question pairs to run against the `dsos` MCP server with a real agent
(Claude Code, Pi — see the root README for setup). Each pair is a **round 1**
(vague, starting question) and a **round 2** (a deeper follow-up that should
reuse round 1's artifacts instead of re-fetching/re-cleaning data).

**How to run one:** open a fresh agent session, paste round 1 as your
message, let it run to a published report. Then open a **new** session and
paste round 2 — the agent should call `search_artifacts` and reuse round 1's
dataset/query artifacts rather than starting over. Reuse is genuine here, not
scripted — a pair failing to reuse cleanly is a real finding about the skill
or the tool descriptions, not the question.

## Demo pairs (Devpost hackathon projects)

These are what the live demo uses. Round 1 should end with the agent
discovering and registering a Devpost hackathon-projects dataset from
scratch — don't pre-fetch it for these.

1. **R1:** What makes a hackathon project win a prize?
   **R2:** Does team size or tech stack matter more for winning?

2. **R1:** What technologies do hackathon teams build with most often?
   **R2:** Has that mix shifted noticeably in the last couple of years?

3. **R1:** How many people are typically on a winning hackathon team?
   **R2:** Do solo projects ever win, and how do they differ from the team
   projects that win?

## Practice pairs (a different dataset, on purpose)

Per the design doc's build rule: sanity-check the discovery skill against a
dataset that isn't the live demo's, so the demo discovery stays genuine
rather than memorized. These use the Stack Overflow Developer Survey (a
dataset considered and dropped earlier in the design process — see the
design doc's dataset discussion) instead of Devpost.

1. **R1:** What programming languages do professional developers use most?
   **R2:** Does language choice correlate with reported job satisfaction or
   compensation?

2. **R1:** How does AI tool adoption change with years of experience?
   **R2:** Do developers who use AI tools report different job satisfaction
   than those who don't?

## Anything else

The discovery skill isn't domain-specific — any public, reasonably-sized
tabular dataset works. Pick something you're curious about, ask a vague
question, then a deeper one that would need the same underlying data.
