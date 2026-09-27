/**
 * Round runner for the dsos-vs-plain-files benchmark (see benchmark/README.md).
 *
 * Spawns a FRESH in-memory agent session per round (the protocol's whole
 * point: no ambient memory), with the tool set fixed by condition:
 *
 *   file — built-in file tools only (read/bash/edit/write/grep/find/ls)
 *   dsos — the same built-ins plus the dsos MCP server's tools, exposed as
 *          custom pi tools. The runner spawns the dsos server itself
 *          (benchmark/mcp_client.mjs, dedicated store benchmark/store.db),
 *          because the pi-mcp-adapter only auto-boots cached/approved
 *          servers in headless sessions. No global config is touched.
 *
 * --hide-raw deletes the raw CSV from the workspace after R1, so rounds
 * 2+ can only reach the data through the store. That is the arm that
 * actually exercises the re-finding claim: with the file sitting in cwd,
 * the file condition's baseline is at its strongest.
 *
 * Workspaces persist per (condition, table) across rounds: benchmark/runs/
 * <cond>/<table>/ — the CSV is copied in for R1 and whatever the agent
 * leaves there stays for R2/R3, identical treatment for both conditions.
 *
 * Usage:
 *   node runner.mjs --condition file --table titanic.csv --rounds 1
 *   node runner.mjs --condition dsos  --tables all --reset-store
 *
 * Results land in benchmark/results/answers_<cond>.json (merged after each
 * run) and benchmark/results/transcripts/<cond>/<table>_R<n>.jsonl.
 */

import { copyFileSync, existsSync, mkdirSync, readFileSync, writeFileSync, rmSync,
         readdirSync, statSync } from "node:fs";
import { join, resolve, dirname, basename } from "node:path";
import { tmpdir } from "node:os";
import { createRequire } from "node:module";
import { pathToFileURL, fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { StdioMcpClient } from "./mcp_client.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(HERE, "..");
const SDK_URL = pathToFileURL(join(
  process.env.USERPROFILE ?? "",
  "AppData/Roaming/npm/node_modules/@earendil-works/pi-coding-agent/dist/index.js"
)).href;

const BUILTIN_TOOLS = ["read", "bash", "edit", "write", "grep", "find", "ls"];
const VENV_PY = join(REPO, ".venv/Scripts/python.exe");
const BENCH_STORE = join(HERE, "store.db");
const ROUND_TIMEOUT_MS = 20 * 60 * 1000;
const TOOL_CALL_TIMEOUT_MS = 10 * 60 * 1000;

// ------------------------------------------------------------------ helpers

const fmt = (n) => (n == null ? "n/a" : (typeof n === "number" ? n.toFixed(2) : n));

function parseArgs(argv) {
  const out = { tables: [], rounds: [1, 2, 3] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--condition") out.condition = argv[++i];
    else if (a === "--table") out.tables.push(argv[++i]);
    else if (a === "--tables") { const v = argv[++i]; if (v === "all") out.allTables = true; else out.tables.push(...v.split(",")); }
    else if (a === "--round" || a === "--rounds") out.rounds = argv[++i].split(",").map(Number);
    else if (a === "--reset-store") out.resetStore = true;
    else if (a === "--keep-raw") out.keepRaw = true;
    else if (a === "--no-file-tools") out.noFileTools = true;
    else if (a === "--manifest") out.manifest = argv[++i];
    else if (a === "--label") out.label = argv[++i];
    else if (a === "--model") out.model = argv[++i];
    else if (a === "--timeout-min") out.timeoutMs = Number(argv[++i]) * 60 * 1000;
    else { console.error(`unknown arg ${a}`); process.exit(2); }
  }
  if (!["file", "dsos"].includes(out.condition)) {
    console.error("usage: node runner.mjs --condition file|dsos [--table x.csv | --tables all]\n"
      + "                       [--rounds 1,2,3] [--reset-store] [--keep-raw] [--no-file-tools]\n"
      + "                       [--manifest results/manifest_chains.json] [--label chain_file]\n"
      + "                       [--model m] [--timeout-min 20]");
    process.exit(2);
  }
  // dsos's intended workflow is register-once-then-rely-on-the-store, so the
  // raw CSV is removed after R1 by default: from R2 on, the store is the only
  // way to the data. Leaving the file in place turns the dsos arm into
  // "files + extra tools" — the agent reads the local file and the store is
  // never exercised, which is what the pre-fix pilot measured.
  out.hideRaw = out.condition === "dsos" && !out.keepRaw;
  return out;
}

function loadManifest(path) {
  return JSON.parse(readFileSync(path ?? join(HERE, "manifest.json"), "utf8"));
}

// Applied identically to both arms. Without it the comparison is a strawman:
// a file-based workflow that computes everything inline and saves nothing has
// no artefact to reuse, so a store would "win" against an agent that was never
// given a reason to persist. Each arm persists in its own idiom (a file, or a
// registered artifact) — the instruction is the same either way.
const PERSIST_HINT =
  "If you produce an intermediate result you would need again (a cleaned table, " +
  "a derived column, a summary), save it now so a later question can reuse it " +
  "instead of redoing the work.";

function promptFor(table, round, workspace) {
  const q = table.rounds.find((r) => r.round === round);
  if (!q) throw new Error(`round ${round} not in manifest for ${table.file_name}`);
  const dataset = round === 1
    ? `I have a dataset at ${join(workspace, table.file_name)}.\n\n`
    : "";
  const chain = q.depends_on && q.depends_on.length
    ? "\n\n(This continues earlier work on this dataset — reuse what you already derived.)"
    : "";
  return `${dataset}${q.question}${chain}\n\nConstraints: ${q.constraints}\n\n${PERSIST_HINT}\n\nGive your final answer in exactly this format: ${q.format}`;
}

function setupWorkspace(label, table) {
  // Outside the repo on purpose. Inside it, the arms shared one find()-able
  // tree: in the pilot an agent read the *other* arm's scratch CSV and, when
  // its own file was gone, curl'd a public copy from GitHub. Also keeps the
  // repo's AGENTS.md (dsos install instructions) out of every arm's context,
  // identically for all of them.
  const workspace = join(tmpdir(), "dsos-bench", label, table.file_name);
  mkdirSync(workspace, { recursive: true });
  const csv = join(workspace, table.file_name);
  if (!existsSync(csv)) copyFileSync(join(HERE, "data", table.file_name), csv);
  return { workspace, csv };
}

// Record what the agent left behind. The file arm's value shows up as
// accumulated scratch, which is otherwise invisible once the run is over.
function snapshotWorkspace(label, table, workspace) {
  const out = [];
  const walk = (dir, prefix) => {
    for (const e of readdirSync(dir, { withFileTypes: true })) {
      const rel = prefix ? `${prefix}/${e.name}` : e.name;
      if (e.isDirectory()) walk(join(dir, e.name), rel);
      else {
        let size = 0;
        try { size = statSync(join(dir, e.name)).size; } catch { /* gone */ }
        out.push(`${String(size).padStart(9)}  ${rel}`);
      }
    }
  };
  try { walk(workspace, ""); } catch { return; }
  const p = join(HERE, "results", "workspaces", `${label}__${table.file_name}.txt`);
  mkdirSync(dirname(p), { recursive: true });
  writeFileSync(p, out.join("\n") + "\n", "utf8");
}

// Relative paths of every file under the workspace. Used to work out which
// files a round *inherited* from an earlier round — the file arm's reuse
// signal. Without this, reading back a cleaned CSV written two rounds ago
// looks identical to redoing the work from scratch.
function listFiles(dir) {
  const out = [];
  const walk = (d, prefix) => {
    for (const e of readdirSync(d, { withFileTypes: true })) {
      const rel = prefix ? `${prefix}/${e.name}` : e.name;
      if (e.isDirectory()) walk(join(d, e.name), rel);
      else out.push(rel);
    }
  };
  try { walk(dir, ""); } catch { /* workspace gone */ }
  return out.sort();
}

// Per-round record of what came in and what was left behind, so the file
// arm's derived-artifact reuse can be scored the same way dsos's
// cross-session artifact reuse is.
function writeCarry(label, table, round, rawName, before, after) {
  const beforeSet = new Set(before);
  const p = join(HERE, "results", "carry", label, `${table.file_name}_R${round}.json`);
  mkdirSync(dirname(p), { recursive: true });
  writeFileSync(p, JSON.stringify({
    raw_file: rawName,
    // Present at round start and not the raw CSV: prior-round derived work.
    // A touch on one of these is reuse, not re-doing the job.
    carried: before.filter((f) => f !== rawName),
    created: after.filter((f) => !beforeSet.has(f)),
  }, null, 2) + "\n", "utf8");
}

// Append this run to results/runs_<label>.json so report.py can pair store
// sessions with rounds exactly, instead of inferring the pairing from the
// console log (which breaks under --label and on a truncated run).
function recordRun(label, entry) {
  const p = join(HERE, "results", `runs_${label}.json`);
  let all = [];
  if (existsSync(p)) { try { all = JSON.parse(readFileSync(p, "utf8")); } catch { all = []; } }
  all.push(entry);
  writeFileSync(p, JSON.stringify(all, null, 2) + "\n");
}

// The dsos server records the session the agent started; the newest one in
// the store belongs to the round that just finished.
function newestStoreSession() {
  const py = join(REPO, ".venv/Scripts/python.exe");
  if (!existsSync(py)) return null;
  const r = spawnSync(py, ["-c",
    "import sqlite3,sys;c=sqlite3.connect(sys.argv[1]);" +
    "r=c.execute(\"select id from sessions where question not like 'bootstrap%' " +
    "order by started_at desc, rowid desc limit 1\").fetchone();" +
    "print(r[0] if r else '')", BENCH_STORE], { encoding: "utf8" });
  const out = (r.stdout || "").trim();
  return out || null;
}

// Rough path extraction from a bash command / tool args, for transcripts.
function pathsFrom(toolName, args) {
  const src = toolName === "bash" ? String(args?.command ?? "")
    : String(args?.path ?? args?.file_path ?? args?.notebook_path ?? "");
  const out = new Set();
  // Path-like tokens, keeping any drive letter (Windows: C:/...).
  for (const m of src.matchAll(/(?:[A-Za-z]:[\/\\])?[\w.\-]+(?:[\/\\][\w.\-]+)+(?:\.[A-Za-z0-9]+)?/g)) {
    out.add(m[0]);
  }
  // Quoted single filenames like "titanic.csv" (no directory component).
  for (const m of src.matchAll(/["']([^"'\s\\/]{2,}\.[A-Za-z0-9]{1,6})["']/g)) out.add(m[1]);
  return [...out];
}

function transcriptEvent(toolName, args) {
  return {
    ts: new Date().toISOString(),
    tool: toolName,
    summary: toolName === "bash" ? String(args?.command ?? "").slice(0, 200)
      : (args?.path ?? args?.file_path ?? JSON.stringify(args ?? {}).slice(0, 120)),
    paths: pathsFrom(toolName, args),
  };
}

function mergeAnswers(condition, key, fields) {
  const p = join(HERE, "results", `answers_${condition}.json`);
  let all = {};
  if (existsSync(p)) { try { all = JSON.parse(readFileSync(p, "utf8")); } catch { all = {}; } }
  const cur = all[key] ?? {};
  all[key] = { ...cur, ...fields };
  writeFileSync(p, JSON.stringify(all, null, 2));
  return p;
}

function isAssistant(m) {
  // pi SDK messages carry `role` ("assistant"), not `type`.
  return (m.role ?? m.type) === "assistant";
}

function sumUsage(messages) {
  let input = 0, output = 0, cost = 0, toolCalls = 0;
  for (const m of messages) {
    if (isAssistant(m) && m.usage) {
      input += m.usage.input ?? 0; output += m.usage.output ?? 0;
      cost += m.usage.cost?.total ?? 0;
      toolCalls += (m.content ?? []).filter((c) => c.type === "toolCall").length;
    }
  }
  return { input, output, cost, toolCalls };
}

function lastAssistantText(messages) {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (isAssistant(m)) {
      const texts = (m.content ?? []).filter((c) => c.type === "text").map((c) => c.text);
      if (texts.length) return texts.join("\n");
    }
  }
  return "";
}

function toolEvents(messages) {
  const events = [];
  for (const m of messages) {
    if (isAssistant(m)) {
      for (const c of m.content ?? []) {
        if (c.type === "toolCall") events.push(transcriptEvent(c.name, c.arguments));
      }
    }
  }
  return events;
}

// ------------------------------------------------- dsos custom-tools bridge

/**
 * Inline pi extension factory: registers every tool of the (already
 * started) dsos MCP client as a first-class pi tool. The MCP inputSchema
 * is a plain JSON-schema object, which is all pi's parameter schemas are.
 *
 * The server's workflow INSTRUCTIONS are attached as promptGuidelines so
 * they reach the model through the system prompt. Two reasons this matters:
 *   - Custom tools are omitted from the system prompt's tool list unless
 *     they carry a promptSnippet, so without these the dsos tools are
 *     effectively invisible outside the raw tool schemas.
 *   - The ordered workflow ("search before fetching anything new") is the
 *     product's core claim; per-tool docstrings say it too, but the pilot
 *     showed a model with only docstrings never called search_artifacts.
 * Dropping the server instructions (as an earlier version of this bridge
 * did) is not a neutral handicap — it understates the real product.
 */
function dsosToolExtension(client) {
  const snippet = "DS Artifact OS — start_session() first, search_artifacts() before fetching or rebuilding anything";
  const guidelines = (client.instructions || "")
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean)
    .slice(0, 12);

  return (pi) => {
    for (const t of client.tools) {
      pi.registerTool({
        name: t.name,
        label: t.name,
        description: t.description ?? "",
        parameters: t.inputSchema ?? { type: "object", properties: {} },
        promptSnippet: snippet,
        // start_session is the mandated first call, so the full workflow
        // rides on it; the rest get the one-line rule only.
        ...(t.name === "start_session" && guidelines.length
          ? { promptGuidelines: guidelines }
          : {}),
        execute: async (_toolCallId, params) => {
          const result = await client.callTool(t.name, params, TOOL_CALL_TIMEOUT_MS);
          const content = (result.content ?? []).filter((c) => c.type === "text" || c.type === "image");
          if (!content.length && result.structuredContent != null) {
            content.push({ type: "text", text: JSON.stringify(result.structuredContent, null, 2) });
          }
          if (result.isError) {
            throw new Error(content.map((c) => c.text ?? "").join("\n") || `dsos tool ${t.name} failed`);
          }
          return { content, details: {} };
        },
      });
    }
  };
}

// -------------------------------------------------------------------- main

const args = parseArgs(process.argv.slice(2));
const MANIFEST = args.manifest ?? join(HERE, "manifest.json");
// --keep-raw writes to its own namespace so the arms never overwrite each other;
// --label additionally keeps a manifest (e.g. the depth chains) from clobbering
// another experiment's answers_*.json.
const LABEL = args.label ?? (args.keepRaw ? `${args.condition}_keepraw` : args.condition);
const manifest = loadManifest(MANIFEST);
const tables = manifest.tables.filter((t) => args.allTables || args.tables.includes(t.file_name));
if (!tables.length) { console.error("no matching tables in manifest"); process.exit(2); }
if (args.resetStore && existsSync(BENCH_STORE)) {
  rmSync(BENCH_STORE); console.log(`reset ${BENCH_STORE}`);
}

const { createAgentSession, SessionManager, DefaultResourceLoader, getAgentDir } =
  await import(SDK_URL);

console.log(`condition=${args.condition}${args.hideRaw ? " (raw removed after R1: store is the only path)" : ""} label=${LABEL} tables=${tables.map((t) => t.file_name).join(", ")} rounds=${args.rounds.join(",")} model=${args.model ?? "default"}`);

for (const table of tables) {
  const { workspace, csv } = setupWorkspace(LABEL, table);
  for (const round of args.rounds) {
    if (args.hideRaw && round > 1 && existsSync(csv)) {
      rmSync(csv);
      console.log(`\n--- hid raw CSV for ${table.file_name} R${round} (store is the only path) ---`);
    }
    const key = `${table.file_name}::R${round}`;
    const prompt = promptFor(table, round, workspace);
    // Snapshot what this round inherits, before the agent can add to it.
    const filesBefore = listFiles(workspace);
    console.log(`\n=== ${key} [${args.condition}] prompt ${prompt.length} chars ===`);

    let client = null, loader = null, extraTools = [];
    if (args.condition === "dsos") {
      const logDir = join(HERE, "results", "transcripts", "dsos_server");
      mkdirSync(logDir, { recursive: true });
      client = new StdioMcpClient({
        command: VENV_PY,
        args: ["-m", "dsos.mcp_server"],
        env: { DSOS_DB_PATH: BENCH_STORE, DSOS_PYTHON_PATH: VENV_PY },
        stderrPath: join(logDir, `${table.file_name}_R${round}.stderr.log`),
      });
      await client.start();
      extraTools = client.tools.map((t) => t.name);
      console.log(`dsos server up: ${extraTools.length} tools registered`);
      loader = new DefaultResourceLoader({
        cwd: workspace,
        agentDir: getAgentDir(),
        extensionFactories: [dsosToolExtension(client)],
      });
      await loader.reload();
    }

    const tools = [
      ...(args.noFileTools ? [] : BUILTIN_TOOLS),
      ...extraTools,
    ];
    const sessionOpts = {
      cwd: workspace,
      tools,
      sessionManager: SessionManager.inMemory(workspace),
      ...(loader ? { resourceLoader: loader } : {}),
    };
    if (args.model) sessionOpts.model = args.model; // never pass model: undefined
    const { session } = await createAgentSession(sessionOpts);
    if (process.env.BENCH_DEBUG_TOOLS) {
      console.log("active tools:", session.getActiveToolNames().sort().join(", "));
    }

    const t0 = Date.now();
    let aborted = false;
    const timer = setTimeout(() => { aborted = true; session.abort(); }, args.timeoutMs ?? ROUND_TIMEOUT_MS);
    try {
      await session.prompt(prompt);
    } catch (e) {
      console.error(`  prompt error: ${e.message}`);
    } finally {
      clearTimeout(timer);
    }

    const messages = session.messages ?? [];
    const usage = sumUsage(messages);
    const answer = lastAssistantText(messages);
    const dur = Math.round((Date.now() - t0) / 1000);
    const usedModel = session.model?.id ?? session.model ?? null;
    session.dispose();
    client?.stop();
    snapshotWorkspace(LABEL, table, workspace);
    writeCarry(LABEL, table, round, table.file_name, filesBefore, listFiles(workspace));

    const tdir = join(HERE, "results", "transcripts", LABEL);
    mkdirSync(tdir, { recursive: true });
    const tpath = join(tdir, `${table.file_name}_R${round}.jsonl`);
    writeFileSync(tpath, toolEvents(messages).map((e) => JSON.stringify(e)).join("\n") + "\n");

    const answersPath = mergeAnswers(LABEL, key, {
      answer_text: answer,
      tokens_in: usage.input,
      tokens_out: usage.output,
      duration_s: dur,
      tool_calls: usage.toolCalls,
      cost_usd: usage.cost,
      aborted,
      model: usedModel,
      transcript: tpath,
    });

    const tags = [...answer.matchAll(/@([\w.\-]+)\[/g)].map((m) => m[1]);
    console.log(`  ${dur}s  in=${usage.input} out=${usage.output} cost=$${fmt(usage.cost)} tools=${usage.toolCalls}${aborted ? "  [TIMED OUT]" : ""}`);
    console.log(`  tags: ${tags.join(", ") || "(none found)"}`);
    console.log(`  answer: ${answer.slice(0, 300).replace(/\n/g, " ")}`);
    console.log(`  answers -> ${answersPath}`);

    recordRun(LABEL, {
      table: table.file_name, round, condition: args.condition, keep_raw: !!args.keepRaw,
      manifest: basename(MANIFEST), model: usedModel, session_id:
        args.condition === "dsos" ? newestStoreSession() : null,
    });
  }
}
console.log("\nbatch done");
