import { z } from "zod";

// Capability changes require an intentional contract update on both sides.
export const HealthResponseSchema = z.strictObject({
  schema_version: z.literal("1.0"),
  status: z.literal("ok"),
  service: z.literal("texas-gto-analysis"),
  version: z.literal("0.1.0"),
  strategy: z.strictObject({
    provider: z.literal("pluribus_style"),
    status: z.literal("not_implemented"),
    supported_games: z.array(z.string()).length(0),
  }),
});

export type HealthResponse = z.infer<typeof HealthResponseSchema>;

export function parseHealthResponse(input: unknown): HealthResponse {
  return HealthResponseSchema.parse(input);
}
