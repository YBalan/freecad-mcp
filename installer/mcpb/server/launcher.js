"use strict";
/*
 * Launcher for the FreeCAD MCP bundle (.mcpb).
 *
 * The bundle ships the freecad-mcp wheel built from the same source tree as the
 * FreeCAD addon, so server and addon always agree on the RPC protocol. This
 * script finds uv, then runs the wheel with `uv tool run`. uv resolves the
 * dependencies and downloads a suitable Python (>= 3.12) when none is installed.
 *
 * Why a launcher instead of calling uv directly from the manifest:
 *  - an optional setting the user left empty reaches us as the literal text
 *    "${user_config.name}", which must never be forwarded as a token or host;
 *  - flags such as --only-text-feedback are only added when enabled;
 *  - uv may not be on the PATH of a GUI app, so common install folders are searched;
 *  - failures are reported on stderr, which the host writes to its MCP log.
 *
 * stdout is reserved for the MCP protocol, so everything here logs to stderr.
 */

const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const IS_WINDOWS = process.platform === "win32";
const PREFIX = "FREECAD_MCP_BUNDLE_";
const PYTHON_REQUEST = ">=3.12";

function log(message) {
  process.stderr.write(`[freecad-mcp bundle] ${message}\n`);
}

/** Read a bundle setting. Empty values and unfilled ${...} placeholders are unset. */
function readSetting(env, name) {
  const raw = env[PREFIX + name];
  if (typeof raw !== "string") return "";
  const value = raw.trim();
  if (value === "" || /^\$\{[^}]*\}$/.test(value)) return "";
  return value;
}

function readFlag(env, name) {
  return ["true", "1", "yes", "on"].includes(readSetting(env, name).toLowerCase());
}

function isFile(candidate) {
  try {
    return fs.statSync(candidate).isFile();
  } catch (_) {
    return false;
  }
}

function pathDirs(env) {
  return (env.PATH || env.Path || "").split(path.delimiter).filter(Boolean);
}

function findOnPath(names, env) {
  for (const dir of pathDirs(env)) {
    for (const name of names) {
      const candidate = path.join(dir, name);
      if (isFile(candidate)) return candidate;
    }
  }
  return null;
}

/** Folders uv's installers use that a GUI app may not have on its PATH yet. */
function uvFallbackDirs(env) {
  const home = os.homedir();
  const dirs = [];
  if (env.UV_INSTALL_DIR) dirs.push(env.UV_INSTALL_DIR);
  dirs.push(path.join(home, ".local", "bin"), path.join(home, ".cargo", "bin"));
  if (IS_WINDOWS) {
    if (env.LOCALAPPDATA) {
      dirs.push(
        path.join(env.LOCALAPPDATA, "Programs", "uv"),
        path.join(env.LOCALAPPDATA, "Microsoft", "WinGet", "Links")
      );
    }
  } else {
    dirs.push("/opt/homebrew/bin", "/usr/local/bin");
  }
  return dirs;
}

function findUv(env) {
  const name = IS_WINDOWS ? "uv.exe" : "uv";
  const onPath = findOnPath([name], env);
  if (onPath) return onPath;
  for (const dir of uvFallbackDirs(env)) {
    const candidate = path.join(dir, name);
    if (isFile(candidate)) return candidate;
  }
  return null;
}

function findWheel(serverDir) {
  let names = [];
  try {
    names = fs.readdirSync(serverDir);
  } catch (_) {
    return null;
  }
  const wheels = names.filter((n) => /^freecad_mcp-.+\.whl$/i.test(n)).sort();
  return wheels.length ? path.join(serverDir, wheels[wheels.length - 1]) : null;
}

/**
 * Headless FreeCAD for execute_code_headless. The server only looks at PATH, but a
 * default Windows install is not on PATH, so look in the standard folders too.
 */
function detectFreecadCmd(env) {
  if (findOnPath(IS_WINDOWS ? ["FreeCADCmd.exe", "freecadcmd.exe"] : ["freecadcmd", "FreeCADCmd"], env)) {
    return ""; // the server finds it by itself
  }
  if (!IS_WINDOWS) return "";
  const roots = [env.ProgramFiles, env["ProgramFiles(x86)"]];
  if (env.LOCALAPPDATA) roots.push(path.join(env.LOCALAPPDATA, "Programs"));
  const found = [];
  for (const root of roots.filter(Boolean)) {
    let entries = [];
    try {
      entries = fs.readdirSync(root);
    } catch (_) {
      continue;
    }
    for (const entry of entries) {
      if (!/^FreeCAD/i.test(entry)) continue;
      const exe = path.join(root, entry, "bin", "FreeCADCmd.exe");
      if (isFile(exe)) found.push({ entry, exe });
    }
  }
  found.sort((a, b) => a.entry.localeCompare(b.entry, undefined, { numeric: true }));
  return found.length ? found[found.length - 1].exe : "";
}

/** Build the child process command line and environment from the host's settings. */
function buildLaunch({ env, uv, wheel }) {
  const args = ["tool", "run", "--python", PYTHON_REQUEST, "--from", wheel, "freecad-mcp"];

  const host = readSetting(env, "HOST");
  if (host) args.push("--host", host);
  if (readFlag(env, "ONLY_TEXT")) args.push("--only-text-feedback");
  const freecadcmd = readSetting(env, "FREECADCMD") || detectFreecadCmd(env);
  if (freecadcmd) args.push("--freecadcmd", freecadcmd);

  const childEnv = { ...env };
  for (const key of Object.keys(childEnv)) {
    if (key.startsWith(PREFIX)) delete childEnv[key];
  }
  // The token goes through the environment, not argv, so it is not visible in the process list.
  const token = readSetting(env, "AUTH_TOKEN");
  if (token) childEnv.FREECAD_MCP_TOKEN = token;

  return { command: uv, args, env: childEnv };
}

function main() {
  const uv = findUv(process.env);
  if (!uv) {
    log("uv was not found. Install it from https://docs.astral.sh/uv/ (Windows: winget install astral-sh.uv),");
    log("then restart Claude Desktop. The FreeCAD MCP Windows installer also installs uv.");
    process.exit(127);
  }
  const wheel = findWheel(__dirname);
  if (!wheel) {
    log(`No freecad_mcp wheel found next to the launcher (${__dirname}). The bundle is damaged; reinstall it.`);
    process.exit(1);
  }

  const launch = buildLaunch({ env: process.env, uv, wheel });
  log(`Starting: ${launch.command} ${launch.args.join(" ")}`);

  const child = spawn(launch.command, launch.args, {
    stdio: "inherit",
    env: launch.env,
    windowsHide: true,
  });
  child.on("error", (error) => {
    log(`Could not start uv: ${error.message}`);
    process.exit(1);
  });
  child.on("exit", (code) => process.exit(code === null ? 1 : code));
  for (const signal of ["SIGINT", "SIGTERM"]) {
    process.on(signal, () => child.kill());
  }
  process.on("exit", () => {
    try {
      child.kill();
    } catch (_) {
      /* already gone */
    }
  });
}

module.exports = { readSetting, readFlag, findUv, findWheel, detectFreecadCmd, buildLaunch };

if (require.main === module) main();
