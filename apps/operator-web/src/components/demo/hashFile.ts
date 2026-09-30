import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";

const DEFAULT_CHUNK_BYTES = 4 * 1024 * 1024;

export async function sha256File(file: File, chunkBytes = DEFAULT_CHUNK_BYTES): Promise<string> {
  if (!Number.isSafeInteger(chunkBytes) || chunkBytes <= 0) throw new Error("chunkBytes must be a positive safe integer");
  const hash = sha256.create();
  for (let offset = 0; offset < file.size; offset += chunkBytes) {
    const chunk = await file.slice(offset, Math.min(offset + chunkBytes, file.size)).arrayBuffer();
    hash.update(new Uint8Array(chunk));
  }
  return bytesToHex(hash.digest());
}
