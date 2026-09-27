/**
 * Minimal stdio JSON-RPC MCP client for the benchmark runner.
 *
 * The runner needs the dsos MCP tools available inside headless pi SDK
 * sessions. The pi-mcp-adapter only boots servers it has cached/approved
 * from interactive sessions, so instead we spawn the dsos server directly
 * and register its tools as custom pi tools (see runner.mjs) — full tool
 * access with no global config or consent state involved.
 */

import { spawn } from "node:child_process";
import { createWriteStream } from "node:fs";

export class StdioMcpClient {
  constructor({ command, args = [], env = {}, cwd, stderrPath }) {
    this.command = command;
    this.args = args;
    this.env = env;
    this.cwd = cwd;
    this.stderrPath = stderrPath;
    this.tools = [];
    this.instructions = "";
    this.nextId = 1;
    this.pending = new Map();
    this.buffer = "";
    this.child = null;
    this.stderrStream = null;
  }

  start() {
    return new Promise((resolve, reject) => {
      this.child = spawn(this.command, this.args, {
        cwd: this.cwd,
        env: { ...process.env, ...this.env },
        stdio: ["pipe", "pipe", "pipe"],
        windowsHide: true,
      });
      if (this.stderrPath) {
        this.stderrStream = createWriteStream(this.stderrPath, { flags: "a" });
      }
      this.child.stderr.setEncoding("utf8");
      this.child.stderr.on("data", (d) => this.stderrStream?.write(d));
      this.child.stdout.setEncoding("utf8");
      this.child.stdout.on("data", (d) => this._onChunk(d));
      this.child.on("error", reject);
      this.child.on("exit", (code) => {
        const err = new Error(`MCP server exited (code ${code})`);
        for (const p of this.pending.values()) p.reject(err);
        this.pending.clear();
      });
      this._request("initialize", {
        protocolVersion: "2024-11-05",
        capabilities: {},
        clientInfo: { name: "dsos-benchmark-runner", version: "0.1" },
      }).then(
        async (result) => {
          this._notify("notifications/initialized");
          // The server's workflow instructions ride on the initialize
          // response. A client may or may not inject those into the model's
          // context; here nothing does unless we do it explicitly, so keep
          // them (see runner.mjs, which feeds them to promptGuidelines).
          this.instructions = result?.instructions ?? "";
          this.tools = await this.listTools();
          resolve(result);
        },
        reject
      );
    });
  }

  _onChunk(chunk) {
    this.buffer += chunk;
    let idx;
    while ((idx = this.buffer.indexOf("\n")) >= 0) {
      const line = this.buffer.slice(0, idx).trim();
      this.buffer = this.buffer.slice(idx + 1);
      if (!line) continue;
      let msg;
      try { msg = JSON.parse(line); } catch { continue; }
      if (msg.id !== undefined && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) reject(new Error(msg.error.message ?? JSON.stringify(msg.error)));
        else resolve(msg.result);
      }
      // Server-initiated notifications are ignored.
    }
  }

  _request(method, params) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      try {
        this.child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
      } catch (e) { reject(e); }
    });
  }

  _notify(method, params) {
    this.child.stdin.write(JSON.stringify({ jsonrpc: "2.0", method, params }) + "\n");
  }

  async listTools() {
    const result = await this._request("tools/list", {});
    return result.tools ?? [];
  }

  callTool(name, args, timeoutMs = 10 * 60 * 1000) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`MCP call ${name} timed out after ${Math.round(timeoutMs / 1000)}s`));
      }, timeoutMs);
      this.pending.set(id, {
        resolve: (v) => { clearTimeout(timer); resolve(v); },
        reject: (e) => { clearTimeout(timer); reject(e); },
      });
      try {
        this.child.stdin.write(
          JSON.stringify({ jsonrpc: "2.0", id, method: "tools/call", params: { name, arguments: args ?? {} } }) + "\n"
        );
      } catch (e) { clearTimeout(timer); reject(e); }
    });
  }

  stop() {
    this.stderrStream?.end();
    if (this.child && this.child.exitCode === null) {
      try { this.child.kill(); } catch { /* already gone */ }
    }
  }
}
