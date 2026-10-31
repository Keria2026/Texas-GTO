import { readFile, writeFile } from "node:fs/promises";
import { z } from "zod";
import { HealthResponseSchema } from "../src/index.ts";

const target = new URL("../schemas/health-response.schema.json", import.meta.url);
const expected = `${JSON.stringify(z.toJSONSchema(HealthResponseSchema), null, 2)}\n`;
if (process.argv.includes("--check")) {
  if ((await readFile(target, "utf8")) !== expected) {
    throw new Error("Protocol schema is stale. Run npm run schema:generate.");
  }
} else {
  await writeFile(target, expected);
}
