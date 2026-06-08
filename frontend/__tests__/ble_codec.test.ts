import { encode, decode } from "../utils/ble_codec";
import { MESSAGE_TYPES } from "../utils/ble_constants";

describe("ble_codec", () => {
  describe("encode", () => {
    it("emits a base64 string with the type byte first and JSON body after", () => {
      const out = encode(MESSAGE_TYPES.PING, { hello: "world" });

      expect(typeof out).toBe("string");
      // base64 alphabet only — no padding-or-alphabet noise
      expect(out).toMatch(/^[A-Za-z0-9+/]+=*$/);

      // Decode the base64 directly (without going through `decode`) so the
      // assertion checks encode's wire format, not the round-trip helper.
      const raw = Uint8Array.from(atob(out), (c) => c.charCodeAt(0));
      expect(raw[0]).toBe(MESSAGE_TYPES.PING);

      const jsonText = new TextDecoder().decode(raw.slice(1));
      expect(JSON.parse(jsonText)).toEqual({ hello: "world" });
    });

    it("defaults data to an empty object when omitted", () => {
      const out = encode(MESSAGE_TYPES.SCAN_START);

      const raw = Uint8Array.from(atob(out), (c) => c.charCodeAt(0));
      expect(raw[0]).toBe(MESSAGE_TYPES.SCAN_START);

      const jsonText = new TextDecoder().decode(raw.slice(1));
      expect(JSON.parse(jsonText)).toEqual({});
    });

    it("preserves the type byte for high values (>= 0x80) without sign extension", () => {
      // Lots of MESSAGE_TYPES live in the 0xF0+ range (SUCCESS/FAILURE) and
      // 0x60+ range (connection/audio). Make sure none of them get corrupted
      // through the Uint8Array.
      for (const type of [MESSAGE_TYPES.SUCCESS, MESSAGE_TYPES.FAILURE, MESSAGE_TYPES.CONNECTION_STATUS_UPDATE]) {
        const out = encode(type, {});
        const raw = Uint8Array.from(atob(out), (c) => c.charCodeAt(0));
        expect(raw[0]).toBe(type);
      }
    });
  });

  describe("decode", () => {
    it("recovers type and parsed JSON", () => {
      // Hand-encoded payload: type=0x62 (SET_LATENCY), body={"speaker_mac":"AA:BB","latency_ms":100}
      const body = JSON.stringify({ speaker_mac: "AA:BB", latency_ms: 100 });
      const bytes = new Uint8Array(1 + body.length);
      bytes[0] = MESSAGE_TYPES.SET_LATENCY;
      bytes.set(new TextEncoder().encode(body), 1);
      const b64 = btoa(String.fromCharCode(...bytes));

      const result = decode(b64);
      expect(result.type).toBe(MESSAGE_TYPES.SET_LATENCY);
      expect(result.json).toEqual({ speaker_mac: "AA:BB", latency_ms: 100 });
    });

    it("treats a single type byte (no body) as an empty JSON object", () => {
      const bytes = new Uint8Array(1);
      bytes[0] = MESSAGE_TYPES.PONG;
      const b64 = btoa(String.fromCharCode(...bytes));

      const result = decode(b64);
      expect(result.type).toBe(MESSAGE_TYPES.PONG);
      expect(result.json).toEqual({});
    });
  });

  describe("roundtrip", () => {
    it("preserves an empty payload", () => {
      const round = decode(encode(MESSAGE_TYPES.PING));
      expect(round).toEqual({ type: MESSAGE_TYPES.PING, json: {} });
    });

    it("preserves a flat payload", () => {
      const payload = { speaker_mac: "11:22:33:44:55:66", volume: 75, is_muted: false };
      const round = decode(encode(MESSAGE_TYPES.SET_VOLUME, payload));
      expect(round.type).toBe(MESSAGE_TYPES.SET_VOLUME);
      expect(round.json).toEqual(payload);
    });

    it("preserves a nested coordinator state payload", () => {
      // Mirror the Slice 3.6 COORDINATOR_STATE shape from ble_constants.ts so
      // the test catches accidental schema drift between the encoder and the
      // CoordinatorState type.
      const payload = {
        tick: 42,
        n_speakers: 2,
        speakers: [
          {
            mac: "AA:AA:AA:AA:AA:AA",
            health: "healthy",
            gain: 1000,
            rssi_dbm: -55,
            rssi_dip_db: 0,
            delay_samples: 0,
          },
          {
            mac: "BB:BB:BB:BB:BB:BB",
            health: "stressed",
            gain: 750,
            rssi_dbm: -78,
            rssi_dip_db: 6,
            delay_samples: 1200,
          },
        ],
      };

      const round = decode(encode(MESSAGE_TYPES.COORDINATOR_STATE, payload));
      expect(round.type).toBe(MESSAGE_TYPES.COORDINATOR_STATE);
      expect(round.json).toEqual(payload);
    });

    it("preserves unicode in string fields", () => {
      // The protocol carries human-typed strings (speaker nicknames,
      // calibration phase labels). UTF-8 must survive the
      // TextEncoder/TextDecoder roundtrip.
      const payload = { nickname: "Loft — kitchen 🎧", phase: "ピンク雑音" };
      const round = decode(encode(MESSAGE_TYPES.CALIBRATION_RESULT, payload));
      expect(round.json).toEqual(payload);
    });

    it("preserves a payload with characters that base64 must escape", () => {
      // `+` and `/` are inside the base64 alphabet; making sure JSON values
      // containing them aren't accidentally rewritten by the encoder.
      const payload = { token: "abc+def/ghi==", note: "a\"b\\c\nd" };
      const round = decode(encode(MESSAGE_TYPES.SET_VOLUME, payload));
      expect(round.json).toEqual(payload);
    });

    it("preserves all MESSAGE_TYPES values as type bytes", () => {
      // Catches the case where someone adds a new MESSAGE_TYPES entry that
      // doesn't survive the byte-prefix encoding (e.g., a multi-byte value
      // or a sign-extended >127 number that gets clipped).
      for (const type of Object.values(MESSAGE_TYPES) as number[]) {
        const round = decode(encode(type, { check: type }));
        expect(round.type).toBe(type);
        expect(round.json).toEqual({ check: type });
      }
    });
  });
});
