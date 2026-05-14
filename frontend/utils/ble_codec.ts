/* utils/ble_codec.ts -------------------------------------------------*/
export function encode(type: number, data: any = {}): string {
    const json = JSON.stringify(data);
    // Size the buffer by UTF-8 byte length, not UTF-16 character count.
    // `json.length` counts code units (a 🎧 is 2, a Japanese char is 1);
    // TextEncoder emits UTF-8 bytes (a 🎧 is 4, a Japanese char is 3), so
    // any non-ASCII payload used to overrun the buffer with RangeError.
    const jsonBytes = new TextEncoder().encode(json);
    const bytes = new Uint8Array(1 + jsonBytes.length);
    bytes[0] = type;
    bytes.set(jsonBytes, 1);
    return btoa(String.fromCharCode(...bytes));
  }
  
  export function decode(b64: string) {
    const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
    const type  = bytes[0];
    const json  = bytes.length > 1 ? JSON.parse(new TextDecoder().decode(bytes.slice(1))) : {};
    return { type, json };
  }