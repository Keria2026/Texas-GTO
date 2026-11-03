import { readFile, writeFile } from "node:fs/promises";
import { z } from "zod";
import {
  HealthResponseSchema, SessionResponseSchema, TrainingJobSchema,
  BlueprintsResponseSchema, AnalysisResponseSchema, EquityResponseSchema,
  ProfilesResponseSchema,
  HandStrengthResponseSchema, AdaptationResponseSchema,
  CoreTrainingResponseSchema,
} from "../src/index.ts";

const schemas = {
  "health-response": HealthResponseSchema,
  "session-response": SessionResponseSchema,
  "training-job": TrainingJobSchema,
  "core-training-response": CoreTrainingResponseSchema,
  "blueprints-response": BlueprintsResponseSchema,
  "analysis-response": AnalysisResponseSchema,
  "equity-response": EquityResponseSchema,
  "profiles-response": ProfilesResponseSchema,
  "hand-strength-response": HandStrengthResponseSchema,
  "adaptation-response": AdaptationResponseSchema,
};
for (const [name, schema] of Object.entries(schemas)) {
  const target = new URL(`../schemas/${name}.schema.json`, import.meta.url);
  const expected = `${JSON.stringify(z.toJSONSchema(schema), null, 2)}\n`;
  if (process.argv.includes("--check")) {
    if ((await readFile(target, "utf8")) !== expected) {
      throw new Error(`${name} schema is stale. Run npm run schema:generate.`);
    }
  } else {
    await writeFile(target, expected);
  }
}
