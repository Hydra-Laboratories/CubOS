import { afterEach, describe, expect, it } from "vitest";
import { sha256File } from "./hashFile";

const originalCrypto = globalThis.crypto;

afterEach(() => {
  Object.defineProperty(globalThis, "crypto", { configurable: true, value: originalCrypto });
});

describe("sha256File", () => {
  it("hashes browser-local file chunks without WebCrypto", async () => {
    Object.defineProperty(globalThis, "crypto", { configurable: true, value: {} });
    const bytes = new TextEncoder().encode("abc");
    const file = {
      size: bytes.length,
      slice: (start: number, end: number) => ({
        arrayBuffer: async () => bytes.slice(start, end).buffer,
      }),
    } as unknown as File;
    expect(await sha256File(file, 2)).toBe("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  });

  it("rejects an invalid chunk size", async () => {
    await expect(sha256File({ size: 0 } as File, 0)).rejects.toThrow("positive safe integer");
  });
});
