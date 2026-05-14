import { readFileSync } from "fs";
import { resolve } from "path";

import { MESSAGE_TYPES } from "../utils/ble_constants";

/**
 * Cross-stack protocol alignment.
 *
 * The frontend and backend each ship their own copy of the BLE wire-protocol
 * type bytes:
 *
 *   - frontend/utils/ble_constants.ts → `MESSAGE_TYPES` (const)
 *   - backend/syncsonic_ble/utils/constants.py → `Msg` IntEnum
 *
 * The encoder writes a 1-byte type prefix and the dispatcher reads the same
 * byte; if these two files drift, requests silently fall through to the
 * unknown handler. The 2026-05-14 BLE_PROTOCOL.md doc already pinned the
 * known drift (START_CLASSIC_PAIRING = 0x66 exists frontend-only). This
 * test makes future drift fail CI instead of falling through silently.
 *
 * The test parses backend/syncsonic_ble/utils/constants.py as text and
 * extracts the IntEnum values. It avoids invoking Python so the frontend
 * Jest job can run it without a Python toolchain on the runner.
 */

const BACKEND_CONSTANTS = resolve(
  __dirname,
  "..",
  "..",
  "backend",
  "syncsonic_ble",
  "utils",
  "constants.py",
);
const BACKEND_HANDLERS = resolve(
  __dirname,
  "..",
  "..",
  "backend",
  "syncsonic_ble",
  "state_change",
  "action_request_handlers.py",
);

/**
 * Extract the `Msg(IntEnum)` member name → hex value mapping from
 * constants.py. Lines look like:
 *
 *     PING                    = 0x01
 *     CONNECT_ONE             = 0x60
 *
 * inside the Msg class. We capture from the class declaration up to the
 * next non-indented line (or end of file).
 */
function parseMsgEnum(): Map<string, number> {
  const source = readFileSync(BACKEND_CONSTANTS, "utf8");
  const lines = source.split(/\r?\n/);
  const startIdx = lines.findIndex((line) => /^class Msg\(IntEnum\):/.test(line));
  if (startIdx === -1) {
    throw new Error("Could not find `class Msg(IntEnum):` in backend constants.py");
  }
  const out = new Map<string, number>();
  for (let i = startIdx + 1; i < lines.length; i++) {
    const line = lines[i];
    if (line.length > 0 && !/^\s/.test(line)) break; // end of class body
    const match = line.match(/^\s+([A-Z_][A-Z0-9_]*)\s*=\s*(0x[0-9a-fA-F]+|\d+)/);
    if (match) {
      out.set(match[1], Number(match[2]));
    }
  }
  return out;
}

/**
 * Extract the request-direction Msg values registered in
 * action_request_handlers.HANDLERS. Lines inside the dict look like:
 *
 *     Msg.PING: handle_ping,
 *     Msg.CONNECT_ONE: handle_connect_one,
 */
function parseHandlersDict(): Set<string> {
  const source = readFileSync(BACKEND_HANDLERS, "utf8");
  const handlersStart = source.indexOf("HANDLERS = {");
  if (handlersStart === -1) {
    throw new Error("Could not find `HANDLERS = {` in backend action_request_handlers.py");
  }
  // Find the matching closing brace.
  let depth = 0;
  let endIdx = -1;
  for (let i = handlersStart + "HANDLERS = ".length; i < source.length; i++) {
    if (source[i] === "{") depth++;
    else if (source[i] === "}") {
      depth--;
      if (depth === 0) {
        endIdx = i;
        break;
      }
    }
  }
  if (endIdx === -1) {
    throw new Error("Could not find closing `}` for HANDLERS dict");
  }
  const body = source.slice(handlersStart, endIdx);
  const out = new Set<string>();
  for (const match of body.matchAll(/Msg\.([A-Z_][A-Z0-9_]*)\s*:/g)) {
    out.add(match[1]);
  }
  return out;
}

describe("protocol alignment between frontend MESSAGE_TYPES and backend Msg IntEnum", () => {
  let backendMsg: Map<string, number>;
  let backendHandlers: Set<string>;

  beforeAll(() => {
    backendMsg = parseMsgEnum();
    backendHandlers = parseHandlersDict();
    if (backendMsg.size === 0) {
      throw new Error(
        "Parsed 0 Msg entries from backend constants.py — parser is broken, fix it",
      );
    }
    if (backendHandlers.size === 0) {
      throw new Error(
        "Parsed 0 HANDLERS entries from backend action_request_handlers.py — parser is broken, fix it",
      );
    }
  });

  it("every backend Msg value has a matching frontend MESSAGE_TYPES entry with the same hex value", () => {
    const mismatches: string[] = [];
    for (const [name, expected] of backendMsg) {
      const actual = (MESSAGE_TYPES as Record<string, number>)[name];
      if (actual === undefined) {
        mismatches.push(`MESSAGE_TYPES.${name} is missing on the frontend (backend value: 0x${expected.toString(16)})`);
      } else if (actual !== expected) {
        mismatches.push(
          `MESSAGE_TYPES.${name} = 0x${actual.toString(16)} but backend Msg.${name} = 0x${expected.toString(16)}`,
        );
      }
    }
    expect(mismatches).toEqual([]);
  });

  it("every frontend MESSAGE_TYPES entry has a backend Msg counterpart, OR is documented as frontend-only in BLE_PROTOCOL.md", () => {
    // The 2026-05-14 BLE_PROTOCOL.md doc explicitly flags START_CLASSIC_PAIRING
    // (0x66) as drift: defined frontend-side but no backend handler or Msg
    // value. Carrying that exception here lets the test fail-fast on any
    // NEW drift while not blocking on the known one. If you intentionally
    // add another frontend-only value, add it here AND document it in
    // docs/BLE_PROTOCOL.md so the gap stays visible to operators.
    const documentedFrontendOnly = new Set(["START_CLASSIC_PAIRING"]);

    const orphans: string[] = [];
    for (const [name, _value] of Object.entries(MESSAGE_TYPES)) {
      if (backendMsg.has(name)) continue;
      if (documentedFrontendOnly.has(name)) continue;
      orphans.push(name);
    }
    expect(orphans).toEqual([]);
  });

  it("every Msg used as a HANDLERS key exists in the Msg IntEnum and in MESSAGE_TYPES", () => {
    // Catches a stale-rename or missing-import in the backend HANDLERS dict
    // before deployment.
    const broken: string[] = [];
    for (const name of backendHandlers) {
      if (!backendMsg.has(name)) {
        broken.push(`HANDLERS uses Msg.${name} but the IntEnum doesn't define it`);
      } else if (!(name in MESSAGE_TYPES)) {
        broken.push(`HANDLERS uses Msg.${name} but MESSAGE_TYPES doesn't declare it`);
      }
    }
    expect(broken).toEqual([]);
  });

  it("documents the historical drift exception so accidental new drift fails the previous test", () => {
    // Sanity check that the exception list above matches the doc. If
    // BLE_PROTOCOL.md gets updated to resolve START_CLASSIC_PAIRING (either
    // by removing it from the frontend or by adding a backend handler),
    // the second test will start passing without this exception — at
    // which point delete this test too.
    const ble_protocol_md = readFileSync(
      resolve(__dirname, "..", "..", "docs", "BLE_PROTOCOL.md"),
      "utf8",
    );
    expect(ble_protocol_md).toMatch(/START_CLASSIC_PAIRING.*0x66.*frontend-only/i);
  });
});
